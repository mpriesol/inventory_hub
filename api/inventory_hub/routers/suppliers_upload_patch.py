from __future__ import annotations
import os
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse

DATA_ROOT = Path(os.getenv("INVENTORY_DATA_ROOT", "C:/!kafe/BikeTrek/web/inventory-data")).resolve()
router = APIRouter()

def _safe_supplier(supplier: str) -> str:
    supplier = supplier.strip().lower()
    if not supplier or any(ch in supplier for ch in "/\\.."):
        raise HTTPException(400, detail="Invalid supplier")
    return supplier

@router.post("/suppliers/{supplier}/feeds/upload")
async def suppliers_upload_feed(supplier: str, file: UploadFile = File(...)):
    supplier = _safe_supplier(supplier)
    if not file or not file.filename:
        raise HTTPException(400, detail="Missing file")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    ext = (Path(file.filename).suffix or ".xml")
    outdir = (DATA_ROOT / "suppliers" / supplier / "feeds" / "xml")
    outdir.mkdir(parents=True, exist_ok=True)
    outpath = outdir / f"uploaded_{ts}{ext}"

    content = await file.read()
    outpath.write_bytes(content)

    rel = str(outpath.relative_to(DATA_ROOT).as_posix())
    return JSONResponse({"saved_path": rel})
