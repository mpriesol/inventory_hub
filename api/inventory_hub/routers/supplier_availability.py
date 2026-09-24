"""Operator-only supplier scheduling and manual queue controls."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.routers.stock_settings import NoStoreRoute
from inventory_hub.services import supplier_availability as service
from inventory_hub.services.supplier_availability_source import AvailabilityError
from inventory_hub.supplier_availability_types import SupplierAvailabilityInput, SupplierAvailabilityRunInput

router = APIRouter(prefix="/supplier-availability", tags=["supplier-availability"], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])
SupplierCode = Annotated[str, Path(pattern=r"^[a-zA-Z0-9_-]{1,50}$")]


async def _call(operation):
    try:
        return await operation
    except AvailabilityError as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.code}) from None


@router.get("")
async def overview(db: AsyncSession = Depends(get_session)):
    return await _call(service.overview(db))


@router.put("/{supplier}")
async def configure(supplier: SupplierCode, payload: SupplierAvailabilityInput, db: AsyncSession = Depends(get_session)):
    return await _call(service.configure(db, supplier, payload))


@router.post("/{supplier}/run", status_code=202)
async def run(supplier: SupplierCode, payload: SupplierAvailabilityRunInput, db: AsyncSession = Depends(get_session)):
    return await _call(service.request_run(db, supplier, payload))
