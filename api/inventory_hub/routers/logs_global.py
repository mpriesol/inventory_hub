from __future__ import annotations

import datetime as dt
import mimetypes
from pathlib import Path
from typing import List, Dict, Any, Iterable, Optional

from fastapi import APIRouter, HTTPException, Query
from ..settings import INVENTORY_DATA_ROOT

router = APIRouter(prefix="/logs", tags=["logs"])

TEXT_EXTS = {".txt", ".log", ".html", ".htm", ".json", ".xml", ".csv"}
MAX_READ_BYTES = 2 * 1024 * 1024  # 2MB

def _suppliers_logs_roots() -> List[Path]:
    base = INVENTORY_DATA_ROOT / "suppliers"
    if not base.exists():
        return []
    out: List[Path] = []
    for sup_dir in base.iterdir():
        if not sup_dir.is_dir():
            continue
        p = sup_dir / "logs"
        if p.exists() and p.is_dir():
            out.append(p)
    return out

def _rel_to_data_root(p: Path) -> str:
    return p.relative_to(INVENTORY_DATA_ROOT).as_posix()

def _gather_logs(allowed_exts: set[str], supplier_filter: Optional[set[str]]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    sup_base = INVENTORY_DATA_ROOT / "suppliers"

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
    if not relpath:
        raise HTTPException(400, detail="relpath is required")

    fp = (INVENTORY_DATA_ROOT / relpath).resolve()

    # bezpečnostná kontrola – musí začínať na INVENTORY_DATA_ROOT/suppliers/*/logs
    try:
        rel = fp.relative_to(INVENTORY_DATA_ROOT / "suppliers")
    except Exception:
        raise HTTPException(400, detail="relpath must be under suppliers/*/logs")

    # navyše musí obsahovať 'logs' v ceste
    if "/logs/" not in rel.as_posix():
        raise HTTPException(400, detail="relpath must point into a logs/ directory")

    if not fp.exists() or not fp.is_file():
        raise HTTPException(404, detail="log not found")

    ext = fp.suffix.lower()
    st = fp.stat()
    size = st.st_size
    mtime_iso = dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")

    if size > MAX_READ_BYTES:
        data = fp.read_bytes()[:MAX_READ_BYTES]
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            text = ""
        return {
            "relpath": _rel_to_data_root(fp),
            "size": size,
            "mtime_iso": mtime_iso,
            "is_html": ext in {".html", ".htm"},
            "truncated": True,
            "text": text,
        }

    if ext in TEXT_EXTS:
        text = fp.read_text(encoding="utf-8", errors="replace")
        return {
            "relpath": _rel_to_data_root(fp),
            "size": size,
            "mtime_iso": mtime_iso,
            "is_html": ext in {".html", ".htm"},
            "truncated": False,
            "text": text,
        }

    # binárny fallback -> pokus o text
    data = fp.read_bytes()[:MAX_READ_BYTES]
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        text = ""
    return {
        "relpath": _rel_to_data_root(fp),
        "size": size,
        "mtime_iso": mtime_iso,
        "is_html": False,
        "truncated": size > MAX_READ_BYTES,
        "text": text,
    }
