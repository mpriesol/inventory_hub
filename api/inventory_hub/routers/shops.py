
from __future__ import annotations
from typing import Dict, Any
from fastapi import APIRouter, Body, HTTPException, UploadFile, File
from inventory_hub.config_io import load_shop as io_load_shop, save_shop as io_save_shop
from inventory_hub.settings import settings
from pathlib import Path

router = APIRouter(tags=["shops"])

def load_shop_config(shop: str) -> Dict[str, Any]:
    return io_load_shop(shop, write_back_on_load=False)

def save_shop_config(shop: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    return io_save_shop(shop, payload)

@router.get("/shops/{shop}/config")
def get_shop_config(shop: str) -> Dict[str, Any]:
    return io_load_shop(shop, write_back_on_load=False)

@router.put("/shops/{shop}/config")
def put_shop_config(shop: str, data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    return io_save_shop(shop, data)

@router.post("/shops/{shop}/upload-export")
async def upload_shop_export(
    shop: str,
    file: UploadFile = File(...),
) -> Dict[str, Any]:
    """
    Nahraj Upgates full export CSV pre daný obchod.
    Uloží sa ako shops/{shop}/latest.csv – toto používa runs/prepare_legacy.
    """
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Očakávam .csv súbor (Upgates full export)")

    shop_dir = settings.INVENTORY_DATA_ROOT / "shops" / shop
    shop_dir.mkdir(parents=True, exist_ok=True)

    content = await file.read()
    if len(content) < 100:
        raise HTTPException(status_code=400, detail="Súbor je príliš malý, skontroluj export")

    # Záloha predchádzajúceho exportu
    latest = shop_dir / "latest.csv"
    if latest.exists():
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        latest.rename(shop_dir / f"latest_{ts}.bak.csv")

    latest.write_bytes(content)

    return {
        "ok": True,
        "shop": shop,
        "saved_as": "latest.csv",
        "size_bytes": len(content),
        "path": f"shops/{shop}/latest.csv",
    }
