
from __future__ import annotations
from typing import Dict, Any
from fastapi import APIRouter, Body, Depends, HTTPException
from inventory_hub.access import admin_access
from inventory_hub.config_io import load_console as io_load_console, save_console as io_save_console
from inventory_hub.config_secrets import ConfigSecretError, config_patch, public_config

router = APIRouter(prefix="/configs", tags=["configs"])

@router.get("/console")
def get_console_config() -> Dict[str, Any]:
    return public_config(io_load_console())

@router.post("/console", dependencies=[Depends(admin_access)])
def post_console_config(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    try:
        patch = config_patch(io_load_console(), payload)
    except ConfigSecretError as error:
        raise HTTPException(422, detail=str(error)) from None
    return public_config(io_save_console(patch))
