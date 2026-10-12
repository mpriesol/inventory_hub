"""Explicit operator-only prefix rotation for future supplier identities."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field

from inventory_hub.access import operator_access
from inventory_hub.config_io import rotate_supplier_prefix
from inventory_hub.routers.stock_settings import NoStoreRoute
from inventory_hub.supplier_prefix import SupplierPrefixError


class PrefixRotation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_prefix: str = Field(pattern=r"^[A-Za-z0-9_-]{1,20}$")
    new_prefix: str = Field(pattern=r"^[A-Za-z0-9_-]{1,20}$")


router = APIRouter(prefix="/suppliers", tags=["supplier-prefixes"], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])


@router.post("/{supplier}/prefix-rotation")
def rotate(supplier: Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,50}$")], payload: PrefixRotation):
    try:
        return rotate_supplier_prefix(supplier, payload.expected_prefix, payload.new_prefix)
    except SupplierPrefixError as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": str(error)}) from None
    except OSError:
        raise HTTPException(503, detail={"code": "supplier_prefix_rotation_unavailable",
            "message": "Prefix rotation could not be completed; verify registry and configuration before further identity operations"}) from None
