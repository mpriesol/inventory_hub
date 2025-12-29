
from __future__ import annotations
from typing import Dict, Any
from fastapi import APIRouter, Body
from inventory_hub.config_io import load_console as io_load_console, save_console as io_save_console

router = APIRouter(prefix="/configs", tags=["configs"])

@router.get("/console")
def get_console_config() -> Dict[str, Any]:
    return io_load_console()

@router.post("/console")
def post_console_config(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    return io_save_console(payload)
