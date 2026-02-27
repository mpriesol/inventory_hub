
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


@router.post("/shops/{shop}/fetch-export")
async def fetch_shop_export(shop: str, export_type: str = "full") -> Dict[str, Any]:
    """
    Stiahni Upgates export z URL nakonfigurovanej v config.json.
    export_type: "full" alebo "small"
    """
    import httpx
    from datetime import datetime

    cfg = io_load_shop(shop, write_back_on_load=False)
    if not cfg:
        raise HTTPException(status_code=404, detail=f"Shop '{shop}' not found")

    url_key = "upgates_full_export_url_csv" if export_type == "full" else "upgates_small_export_url_csv"
    url = cfg.get(url_key, "")
    if not url:
        raise HTTPException(status_code=400, detail=f"URL pre '{export_type}' export nie je nakonfigurovaná")

    shop_dir = settings.INVENTORY_DATA_ROOT / "shops" / shop
    shop_dir.mkdir(parents=True, exist_ok=True)

    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            content = resp.content
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Chyba pri sťahovaní exportu: {e}")

    if len(content) < 100:
        raise HTTPException(status_code=502, detail="Stiahnutý súbor je príliš malý")

    # Záloha predchádzajúceho
    latest = shop_dir / "latest.csv"
    if latest.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        latest.rename(shop_dir / f"latest_{ts}.bak.csv")

    latest.write_bytes(content)

    return {
        "ok": True,
        "shop": shop,
        "export_type": export_type,
        "url": url,
        "size_bytes": len(content),
        "saved_as": "latest.csv",
    }
