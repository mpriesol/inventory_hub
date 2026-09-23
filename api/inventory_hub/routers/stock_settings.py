"""Operator-only configuration; responses and validation errors are not cached."""
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession
from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.stock_settings_types import WarehouseSettingsInput, ShopSettingsInput
from inventory_hub.services import stock_settings as service


class NoStoreRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handler(request):
            try:
                response = await original(request)
            except HTTPException as error:
                error.headers = {**(error.headers or {}), "Cache-Control": "no-store"}
                raise
            except RequestValidationError:
                response = JSONResponse(status_code=422, content={"detail": {
                    "code": "stock_settings_invalid_request", "message": "stock_settings_invalid_request"}})
            response.headers["Cache-Control"] = "no-store"
            return response
        return handler


router = APIRouter(prefix="/stock-settings", tags=["stock-settings"], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])
ShopCode = Annotated[str, Query(pattern=r"^[a-z0-9][a-z0-9_-]{0,49}$")]
WarehouseCode = Annotated[str, Query(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,49}$")]


async def _call(operation):
    try:
        return await operation
    except service.SettingsError as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.code}) from None


@router.get("/options")
async def options(shop_code: ShopCode, db: AsyncSession = Depends(get_session)):
    return await _call(service.options(db, shop_code))


@router.get("/warehouse")
async def warehouse(warehouse_code: WarehouseCode, db: AsyncSession = Depends(get_session)):
    return await _call(service.warehouse_settings(db, warehouse_code))


@router.post("/warehouse")
async def configure_warehouse(payload: WarehouseSettingsInput, db: AsyncSession = Depends(get_session)):
    return await _call(service.configure_warehouse(db, payload))


@router.post("/shop")
async def configure_shop(payload: ShopSettingsInput, db: AsyncSession = Depends(get_session)):
    return await _call(service.configure_shop(db, payload))
