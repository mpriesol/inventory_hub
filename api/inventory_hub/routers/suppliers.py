
from __future__ import annotations
from typing import Dict, Any
from fastapi import APIRouter, Body, HTTPException
from inventory_hub.config_io import load_supplier as io_load_supplier, save_supplier as io_save_supplier

router = APIRouter(tags=["suppliers"])

@router.get("/suppliers/{supplier}/config")
def get_supplier_config(supplier: str) -> Dict[str, Any]:
    return io_load_supplier(supplier, write_back_on_load=True)

@router.put("/suppliers/{supplier}/config")
@router.post("/suppliers/{supplier}/config")
def put_supplier_config(supplier: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    return io_save_supplier(supplier, payload or {})
