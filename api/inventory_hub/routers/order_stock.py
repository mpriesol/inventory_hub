"""Explicit operator-controlled stock processing, without a background importer."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.order_stock_types import OrderStockApplyRequest, OrderStockConfigureRequest, OrderStockPreviewRequest
from inventory_hub.services import order_stock as service
from inventory_hub.services.order_stock_source import SourceError


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
                    "code": "order_stock_invalid_request", "message": "order_stock_invalid_request",
                }})
            response.headers["Cache-Control"] = "no-store"
            return response

        return handler


router = APIRouter(prefix="/order-stock", tags=["order-stock"], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])
ShopCode = Annotated[str, Query(pattern=r"^[a-z0-9][a-z0-9_-]{0,49}$")]


async def _call(operation):
    try:
        return await operation
    except (service.OrderStockError, SourceError) as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.code}) from None


@router.get("/options")
async def options(shop_code: ShopCode, db: AsyncSession = Depends(get_session)):
    return await _call(service.options(db, shop_code))


@router.post("/configure")
async def configure(payload: OrderStockConfigureRequest, db: AsyncSession = Depends(get_session)):
    return await _call(service.configure(db, payload))


@router.post("/preview")
async def preview(payload: OrderStockPreviewRequest, db: AsyncSession = Depends(get_session)):
    return await _call(service.preview(db, payload))


@router.get("/previews")
async def list_previews(shop_code: ShopCode, db: AsyncSession = Depends(get_session)):
    return await _call(service.list_previews(db, shop_code))


@router.get("/previews/{preview_id}")
async def get_preview(preview_id: UUID, db: AsyncSession = Depends(get_session)):
    return await _call(service.get_preview(db, str(preview_id)))


@router.post("/previews/{preview_id}/apply")
async def apply(preview_id: UUID, payload: OrderStockApplyRequest, db: AsyncSession = Depends(get_session)):
    return await _call(service.apply(db, str(preview_id), payload))
