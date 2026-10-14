from __future__ import annotations

import datetime as dt
import mimetypes
from pathlib import Path
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, HTTPException, Query
from ..settings import INVENTORY_DATA_ROOT
from inventory_hub.file_access import log_path, read_log_document

router = APIRouter(prefix="/logs", tags=["logs"])

MAX_READ_BYTES = 2 * 1024 * 1024  # 2MB

def _suppliers_logs_roots() -> List[Path]:
    base = INVENTORY_DATA_ROOT / "suppliers"
    if not base.exists():
        return []
    out: List[Path] = []
    for sup_dir in base.iterdir():
        if sup_dir.is_symlink() or not sup_dir.is_dir():
            continue
        p = sup_dir / "logs"
        if not p.is_symlink() and p.exists() and p.is_dir():
            out.append(p)
    return out

def _rel_to_data_root(p: Path) -> str:
    return p.relative_to(INVENTORY_DATA_ROOT).as_posix()

def _gather_logs(allowed_exts: set[str], supplier_filter: Optional[set[str]]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []

    for logs_root in _suppliers_logs_roots():
        # ak je filter, skontroluj supplier kód
        try:
            supplier_code = logs_root.parent.name
        except Exception:
            continue
        if supplier_filter and supplier_code not in supplier_filter:
            continue

        for fp in logs_root.rglob("*"):
            if not fp.is_file():
                continue
            ext = fp.suffix.lower()
            if allowed_exts and ext not in allowed_exts:
                continue
            try:
                fp = log_path(INVENTORY_DATA_ROOT, _rel_to_data_root(fp))
                st = fp.stat()
            except Exception:
                continue
            items.append({
                "relpath": _rel_to_data_root(fp),
                "supplier": supplier_code,
                "filename": fp.name,
                "size": st.st_size,
                "mtime_iso": dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                "ext": ext,
                "content_type": mimetypes.guess_type(fp.name)[0] or "application/octet-stream",
            })
    return items

@router.get("/recent")
def get_recent_logs(
    limit: int = Query(100, ge=1, le=1000),
    exts: str = Query("html,htm,log,txt,json,xml,csv"),
    supplier: Optional[str] = Query(None, description="Comma-separated supplier codes to include"),
):
    """
    Globálny výpis posledných logov naprieč všetkými suppliers/*/logs.
    Voliteľne filter: ?supplier=paul-lange,zookee
    """
    allow_exts = {"." + e.strip().lower() for e in exts.split(",") if e.strip()}
    supplier_filter = {s.strip() for s in supplier.split(",")} if supplier else None

    items = _gather_logs(allow_exts, supplier_filter)
    items.sort(key=lambda x: x["mtime_iso"], reverse=True)
    return {"items": items[:limit]}

@router.get("/read")
def read_log(relpath: str):
    """
    Prečíta konkrétny log podľa relpath (voči INVENTORY_DATA_ROOT).
    Povolené súbory len pod suppliers/*/logs.
    """
    return read_log_document(INVENTORY_DATA_ROOT, relpath, max_bytes=MAX_READ_BYTES)
