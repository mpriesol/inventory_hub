
from __future__ import annotations
from typing import Dict, Any
from json import JSONDecodeError
from fastapi import APIRouter, Body, HTTPException
from inventory_hub.config_io import (
    load_shop as io_load_shop,
    load_shop_strict as io_load_shop_strict,
    save_shop as io_save_shop,
    shop_path,
)

router = APIRouter(tags=["shops"])

def load_shop_config(shop: str) -> Dict[str, Any]:
    return io_load_shop(shop, write_back_on_load=False)

def save_shop_config(shop: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    return io_save_shop(shop, payload)

@router.get("/shops/{shop}/config")
def get_shop_config(shop: str) -> Dict[str, Any]:
    try:
        return io_load_shop_strict(shop)
    except FileNotFoundError:
        path = shop_path(shop).resolve()
        raise HTTPException(status_code=404, detail=f"Config not found: {path}")
    except JSONDecodeError as exc:
        path = shop_path(shop).resolve()
        raise HTTPException(
            status_code=422,
            detail=f"Invalid JSON in {path} at line {exc.lineno} column {exc.colno}",
        )

@router.put("/shops/{shop}/config")
def put_shop_config(shop: str, data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    return io_save_shop(shop, data)
