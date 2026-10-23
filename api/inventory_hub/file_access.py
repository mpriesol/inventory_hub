"""Private, bounded access to operator documents, never to the data directory itself.

Authentication is enforced by the application middleware. This separate allowlist
also applies to authenticated users: config, credentials and browser state are
not downloadable assets. Keep all path-based file endpoints on this boundary.
"""
from __future__ import annotations

import mimetypes
import re
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote

from fastapi import HTTPException
from starlette.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles


_DOCUMENT_SUFFIXES = {".csv", ".tsv", ".xml", ".json", ".pdf", ".xlsx", ".xls", ".doc", ".docx",
                      ".txt", ".log", ".html", ".htm", ".feed"}
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp"}
_PRIVATE_PART = re.compile(
    r"^(?:config(?:uration)?|credentials?|secrets?|storage[_-]?state|state|"
    r"browser(?:[_-]?state)?|cookies?|sessions?|tokens?|passwords?|auth|headers|network)(?:[._-].*)?$", re.I
)
_SECURITY_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "Referrer-Policy": "no-referrer",
}


def _asset_location(parts: tuple[str, ...]) -> bool:
    if len(parts) >= 4 and parts[0] == "suppliers":
        area = parts[2]
        if area == "logs" or area == "feeds_converted":
            return True
        if len(parts) >= 5:
            return (area == "invoices" and parts[3] in {"raw", "pdf", "csv", "history"}
                    or area == "feeds" and parts[3] in {"raw", "xml", "csv", "converted", "samples", "mapping"}
                    or area == "imports" and parts[3] in {"upgates", "upgates_selected", "sent"})
    if len(parts) >= 3 and parts[0] == "shops":
        return (len(parts) == 3 and parts[2] == "latest.csv"
                or len(parts) >= 4 and parts[2] in {"imports", "exports", "logs"})
    return len(parts) == 2 and parts[0] == "logs" and parts[1].endswith(".log")


def document_path(root: Path, relpath: str) -> Path:
    """Resolve an allowed document, rejecting every symlink component and escape."""
    value = unquote(relpath)
    parts = PurePosixPath(value).parts
    if (not value or value.startswith("/") or "\\" in value or ":" in value
            or any(ord(char) < 32 for char in value) or any(part == ".." for part in parts)):
        raise HTTPException(400, detail="Invalid document path")
    if (not parts or any(part.startswith(".") or part.lower() == "state"
                         or _PRIVATE_PART.fullmatch(part) for part in parts)
            or not _asset_location(parts)
            or PurePosixPath(value).suffix.lower() not in _DOCUMENT_SUFFIXES | _IMAGE_SUFFIXES):
        raise HTTPException(404, detail="Document not found")
    root = Path(root).expanduser().resolve()
    candidate = root
    for part in parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise HTTPException(404, detail="Document not found")
    target = candidate.resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(404, detail="Document not found")
    return target


def stored_document_path(root: Path, path: str | Path) -> Path:
    """Validate a stored absolute invoice filename through the same boundary."""
    target = Path(path)
    if target.is_absolute():
        try:
            path = target.relative_to(Path(root).expanduser().resolve()).as_posix()
        except ValueError:
            raise HTTPException(404, detail="Document not found") from None
    return document_path(root, str(path))


def document_response(target: Path, *, disposition: str = "attachment",
                      filename: str | None = None, stable: bool = False) -> Response:
    # Never interpret downloaded HTML/XML/JSON as a same-origin active document.
    suffix = target.suffix.lower()
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if suffix in {".html", ".htm", ".xml", ".json", ".txt", ".log"}:
        media_type = "text/plain"
    if suffix not in _IMAGE_SUFFIXES | {".pdf", ".txt", ".log"}:
        disposition = "attachment"
    name = (filename or target.name).replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if ord(char) >= 32 and ord(char) != 127) or target.name
    # RFC 5987 encoding keeps quotes, newlines and Unicode out of header syntax.
    headers = {**_SECURITY_HEADERS, "Content-Disposition":
               f"{disposition}; filename*=utf-8''{quote(name, safe='')}"}
    if stable or suffix in {".log", ".txt"}:
        return Response(target.read_bytes(), media_type=media_type, headers=headers)
    return FileResponse(target, media_type=media_type, headers=headers)


class PrivateDataFiles(StaticFiles):
    """Compatibility for existing /data links, with the same document policy."""
    async def get_response(self, path: str, scope) -> Response:
        if scope["method"] not in {"GET", "HEAD"}:
            raise HTTPException(405, detail="Method not allowed")
        target = document_path(Path(self.directory), path)
        return document_response(target, disposition="inline")


def log_path(root: Path, relpath: str, *, supplier: str | None = None) -> Path:
    target = document_path(root, relpath)
    parts = target.relative_to(Path(root).expanduser().resolve()).parts
    if (len(parts) < 4 or parts[0] != "suppliers" or parts[2] != "logs"
            or supplier is not None and parts[1] != supplier):
        raise HTTPException(404, detail="Log not found")
    return target


def read_log_document(root: Path, relpath: str, *, supplier: str | None = None,
                      max_bytes: int = 2 * 1024 * 1024) -> dict:
    from datetime import datetime
    target = log_path(root, relpath, supplier=supplier)
    stat = target.stat()
    with target.open("rb") as source:
        content = source.read(max_bytes)
    return {
        "relpath": target.relative_to(Path(root).expanduser().resolve()).as_posix(),
        "size": stat.st_size,
        "mtime_iso": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        # Log HTML is untrusted supplier content; show it as inert text in the UI.
        "is_html": False,
        "truncated": stat.st_size > max_bytes,
        "text": content.decode("utf-8", errors="replace"),
    }


def local_feed_path(root: Path, path: str | Path) -> Path:
    """Local feeds must already be in a feed asset area, never config or state."""
    target = stored_document_path(root, path)
    parts = target.relative_to(Path(root).expanduser().resolve()).parts
    if (len(parts) < 5 or parts[0] != "suppliers" or parts[2] != "feeds"
            or target.suffix.lower() not in {".xml", ".csv", ".tsv", ".json", ".feed"}):
        raise HTTPException(404, detail="Feed source not found")
    return target


def filename_component(value: str) -> str:
    """A literal filename stem, never a path, encoded path or glob expression."""
    if (not isinstance(value, str) or not value or value in {".", ".."}
            or value.startswith(".") or unquote(value) != value
            or any(char in value for char in "/\\:*?[]")
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise HTTPException(400, detail="Invalid file reference")
    return value


def code_component(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise HTTPException(400, detail="Invalid supplier or shop reference")
    return value


def confined_path(root: Path, relative: str) -> Path:
    """Internal read/write path, allowing absent files but no symlink hops.

    Unlike document_path this does not authorize download. Callers construct its
    relative path from fixed internal directories and validated components.
    """
    parts = PurePosixPath(relative).parts
    if (not relative or relative.startswith("/") or "\\" in relative or ":" in relative
            or unquote(relative) != relative or ".." in parts
            or any(ord(char) < 32 for char in relative)):
        raise HTTPException(400, detail="Invalid file reference")
    root = Path(root).expanduser().resolve()
    candidate = root
    for part in parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise HTTPException(400, detail="Invalid file reference")
    if not candidate.resolve().is_relative_to(root):
        raise HTTPException(400, detail="Invalid file reference")
    return candidate
