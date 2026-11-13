"""Bounded operational events shared by API requests and background workers.

Only machine identifiers and numbers belong here, never payloads, credentials,
URLs or exception messages. This disposable diagnostic history is not an audit
ledger. A process lock coordinates rotation/readers across local workers.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from inventory_hub.settings import settings

MAX_BYTES = 5_000_000
BACKUPS = 3
MAX_LINE = 4096
_context = ContextVar("operation_context", default={})
_codes = {"component", "event", "supplier", "phase", "error_code", "reason"}
_numbers = {"run_id", "status_code", "duration_ms", "page", "observed_page", "rows", "accepted", "rejected", "unknown", "redirect"}


class LogUnavailable(Exception):
    pass


class CursorExpired(Exception):
    pass


@contextmanager
def context(**fields):
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def _clean(fields):
    result = {}
    for key, value in fields.items():
        if key in _codes and isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,100}", value):
            result[key] = value
        elif key in _numbers and type(value) is int and 0 <= value <= 10**12:
            result[key] = value
        elif key in {"id", "request_id"} and isinstance(value, str) and re.fullmatch(r"[a-f0-9]{32}", value):
            result[key] = value
        elif key == "level" and value in ("info", "warning", "error"):
            result[key] = value
        elif key == "direction" and value in ("inbound", "outbound", "internal"):
            result[key] = value
        elif key == "method" and value in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            result[key] = value
        elif key == "target" and isinstance(value, str) and re.fullmatch(r"[/a-zA-Z0-9_{}:.-]{1,250}", value):
            result[key] = value
        elif key == "timestamp" and isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", value):
            result[key] = value
    return result


def _directory():
    directory = Path(settings.INVENTORY_DATA_ROOT) / "logs"
    if directory.is_symlink():
        raise LogUnavailable()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@contextmanager
def _locked(directory, exclusive):
    fd = os.open(directory / "operations.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        os.close(fd)


def emit(event, *, component=None, level="info", **fields):
    """Best effort: diagnostics must not break a business operation."""
    try:
        entry = _clean({**_context.get(), **fields, "event": event, "level": level,
                        **({"component": component} if component else {})})
        entry.update(id=uuid4().hex, timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
        line = (json.dumps(entry, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
        if len(line) > MAX_LINE:
            return False
        directory = _directory()
        with _locked(directory, True):
            paths = [directory / ("operations.jsonl" + (f".{i}" if i else "")) for i in range(BACKUPS + 1)]
            if any(p.is_symlink() for p in paths):
                return False
            if paths[0].exists() and paths[0].stat().st_size + len(line) > MAX_BYTES:
                for i in range(BACKUPS, 0, -1):
                    if paths[i - 1].exists():
                        os.replace(paths[i - 1], paths[i])
            fd = os.open(paths[0], os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "ab") as stream:
                stream.write(line)
        return True
    except (OSError, ValueError, TypeError, LogUnavailable):
        return False


def read(*, limit=100, before=None, component=None, supplier=None, level=None,
         run_id=None, request_id=None, since=None, until=None):
    """Newest first, stable cursor across new writes; expired cursors are explicit."""
    try:
        chunks = []
        directory = _directory()
        with _locked(directory, False):
            for i in range(BACKUPS + 1):
                path = directory / ("operations.jsonl" + (f".{i}" if i else ""))
                try:
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                except FileNotFoundError:
                    continue
                with os.fdopen(fd, "rb") as stream:
                    # Bound reads even if an external process damaged a file.
                    excess = max(0, os.fstat(stream.fileno()).st_size - MAX_BYTES)
                    stream.seek(excess)
                    chunk = stream.read(MAX_BYTES)
                    chunks.append(chunk.split(b"\n", 1)[-1] if excess else chunk)
    except (OSError, LogUnavailable):
        raise LogUnavailable() from None
    filters = {k: v for k, v in {"component": component, "supplier": supplier, "level": level,
                               "run_id": run_id, "request_id": request_id}.items() if v is not None}
    items, found = [], before is None
    for chunk in chunks:
        for raw in reversed(chunk.splitlines()):
            if len(raw) > MAX_LINE:
                continue
            try:
                parsed = json.loads(raw)
                entry = _clean(parsed) if isinstance(parsed, dict) else {}
            except (ValueError, UnicodeError):
                continue
            if not all(k in entry for k in ("id", "timestamp", "event", "component")):
                continue
            if not found:
                found = entry["id"] == before
                continue
            if any(entry.get(k) != v for k, v in filters.items()):
                continue
            if since and entry["timestamp"] < since or until and entry["timestamp"] > until:
                continue
            items.append(entry)
            if len(items) > limit:
                return {"items": items[:limit], "next_cursor": items[limit - 1]["id"], "max_bytes": MAX_BYTES * (BACKUPS + 1)}
    if not found:
        raise CursorExpired()
    return {"items": items, "next_cursor": None, "max_bytes": MAX_BYTES * (BACKUPS + 1)}
