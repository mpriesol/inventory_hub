
from __future__ import annotations
from typing import Dict, Any
from fastapi import APIRouter, Body, HTTPException
from inventory_hub.config_io import load_shop as io_load_shop, save_shop as io_save_shop

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
