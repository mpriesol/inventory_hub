from __future__ import annotations
import csv
import io
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

DATA_ROOT = Path(os.getenv("INVENTORY_DATA_ROOT", "C:/!kafe/BikeTrek/web/inventory-data")).resolve()

router = APIRouter()

def _safe_path(relpath: str) -> Path:
    p = (DATA_ROOT / relpath).resolve()
    if not str(p).startswith(str(DATA_ROOT)):
        raise HTTPException(status_code=400, detail="Invalid path")
    return p

@router.api_route("/files/download", methods=["GET", "HEAD"])
def files_download(relpath: str):
    fp = _safe_path(relpath)
    if not fp.exists() or not fp.is_file():
        raise HTTPException(404, detail="File not found")
    return FileResponse(path=fp, filename=fp.name, media_type="application/octet-stream")

@router.get("/files/stat")
def files_stat(relpath: str):
    fp = _safe_path(relpath)
    if not fp.exists() or not fp.is_file():
        raise HTTPException(404, detail="File not found")
    st = fp.stat()
    return {"relpath": relpath, "size": st.st_size, "mtime": st.st_mtime,
            "mtime_iso": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()}

@router.get("/files/preview")
def files_preview(relpath: str, limit: int = Query(120, ge=1, le=1000)):
    fp = _safe_path(relpath)
    if not fp.exists() or not fp.is_file():
        raise HTTPException(404, detail="File not found")

    raw = fp.read_bytes()
    head = raw[:65536]
    sample_text = head.decode("utf-8-sig", errors="replace")

    try:
        dialect = csv.Sniffer().sniff(sample_text, delimiters=[",",";","\t","|"])
        delimiter = dialect.delimiter
    except Exception:
        counts = {d: sum(line.count(d) for line in sample_text.splitlines()[:50]) for d in [",",";","\t","|"]}
        delimiter = max(counts, key=counts.get)

    rows = []
    with io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f, delimiter=delimiter)
        for i, row in enumerate(reader):
            rows.append([str(c) for c in row])
            if i >= (limit - 1):
                break

    return JSONResponse(rows)
