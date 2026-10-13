"""Server-only persistent Sloger login, separate from public supplier files.

The deployment supplies a private persistent mount. Credentials are protected by
OS ownership/permissions, not encrypted; only this service reads their contents.
"""
import json
import os
from pathlib import Path
import stat
import tempfile

from inventory_hub.services.catalog import CatalogError
from inventory_hub.settings import settings
from inventory_hub.sloger_probe_types import SlogerCredentials


MAX_BYTES = 8192
FILENAME = "sloger.json"


def _unavailable():
    return CatalogError("sloger_credentials_unavailable", "Stored Sloger credentials are unavailable", 503)


def _root(*, create=False) -> Path:
    root = settings.SUPPLIER_CREDENTIALS_ROOT
    public = settings.INVENTORY_DATA_ROOT.resolve()
    resolved = root.resolve()
    if (not root.is_absolute() or resolved == public or public in resolved.parents
            or resolved in public.parents or root.is_symlink()):
        raise _unavailable()
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise _unavailable()
    # A fresh Docker named volume starts with 0755. Seal this owned private
    # directory before any credential read/write, without relying on SSH uid.
    if stat.S_IMODE(info.st_mode) != 0o700:
        root.chmod(0o700)
    return root


def _read() -> SlogerCredentials | None:
    try:
        root = _root()
    except FileNotFoundError:
        return None
    try:
        fd = os.open(root / FILENAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > MAX_BYTES):
            raise _unavailable()
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise _unavailable()
    return SlogerCredentials.model_validate_json(raw)


def load() -> SlogerCredentials:
    try:
        credentials = _read()
    except Exception:
        # Validation errors may contain complete input; never propagate them.
        raise _unavailable() from None
    if credentials is None:
        raise CatalogError("sloger_credentials_not_configured", "Save the Sloger login before starting a lookup", 409)
    return credentials


def status() -> dict:
    try:
        credentials = _read()
        return {"configured": credentials is not None, "username": credentials.username if credentials else None}
    except Exception:
        raise _unavailable() from None


def save(credentials: SlogerCredentials) -> dict:
    temporary = None
    try:
        root = _root(create=True)
        destination = root / FILENAME
        if destination.is_symlink():
            raise _unavailable()
        payload = json.dumps({"username": credentials.username,
                              "password": credentials.password.get_secret_value()}, ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_BYTES:
            raise _unavailable()
        fd, temporary = tempfile.mkstemp(prefix=".sloger-", dir=root)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return {"configured": True, "username": credentials.username}
    except Exception:
        raise _unavailable() from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
