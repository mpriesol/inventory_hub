from __future__ import annotations

import datetime as dt
import mimetypes
from pathlib import Path
from typing import List, Dict, Any

from fastapi import APIRouter, HTTPException, Query
from ..settings import INVENTORY_DATA_ROOT
from inventory_hub.file_access import log_path, read_log_document

router = APIRouter(prefix="/suppliers/{supplier}/logs", tags=["logs"])

MAX_READ_BYTES = 2 * 1024 * 1024  # 2 MB hard limit


def _supplier_logs_dir(supplier: str) -> Path:
    p = INVENTORY_DATA_ROOT / "suppliers" / supplier / "logs"
    return p


def _rel_to_data_root(p: Path) -> str:
    return p.relative_to(INVENTORY_DATA_ROOT).as_posix()


@router.get("/recent")
def get_recent_logs(
    supplier: str,
    limit: int = Query(50, ge=1, le=500),
    exts: str = Query("html,htm,log,txt,json,xml,csv"),
):
    """
    Vráti posledné logy (rekurzívne) z suppliers/<supplier>/logs.
    """
    logs_dir = _supplier_logs_dir(supplier)
    if not logs_dir.exists():
        return {"items": []}

    allow_exts = {"." + e.strip().lower() for e in exts.split(",") if e.strip()}
    items: List[Dict[str, Any]] = []

    for fp in logs_dir.rglob("*"):
        if not fp.is_file():
            continue
        ext = fp.suffix.lower()
        if allow_exts and ext not in allow_exts:
            continue
        try:
            fp = log_path(INVENTORY_DATA_ROOT, _rel_to_data_root(fp), supplier=supplier)
            stat = fp.stat()
        except Exception:
            continue
        items.append({
            "relpath": _rel_to_data_root(fp),
            "filename": fp.name,
            "size": stat.st_size,
            "mtime_iso": dt.datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            "ext": ext,
            "content_type": mimetypes.guess_type(fp.name)[0] or "application/octet-stream",
        })

    # sort by mtime desc
    items.sort(key=lambda x: x["mtime_iso"], reverse=True)
    return {"items": items[:limit]}


@router.get("/read")
def read_log(supplier: str, relpath: str):
    """
    Prečíta obsah textových/HTML logov do 2 MB. Pre HTML vráti text ako string.
    """
    return read_log_document(INVENTORY_DATA_ROOT, relpath, supplier=supplier, max_bytes=MAX_READ_BYTES)
