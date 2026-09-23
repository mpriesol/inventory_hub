"""Protected collection controls, sanitized inbox and read-only stock drafts."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.order_collection_types import CollectionConfigure, CollectionConfirmation, StockProjectionRequest
from inventory_hub.services import order_collection as service, stock_projection


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
                    "code": "order_collection_invalid_request", "message": "order_collection_invalid_request"}})
            response.headers["Cache-Control"] = "no-store"
            return response
        return handler


router = APIRouter(prefix="/order-collection", tags=["order-collection"], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])
ShopCode = Annotated[str, Query(pattern=r"^[a-z0-9][a-z0-9_-]{0,49}$")]


async def _call(operation):
    try:
        return await operation
    except (service.CollectionError, stock_projection.StockProjectionError) as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.code}) from None


@router.get("/status")
async def status(shop_code: ShopCode, db: AsyncSession = Depends(get_session)):
    return await _call(service.status(db, shop_code))


@router.post("/configure")
async def configure(payload: CollectionConfigure, db: AsyncSession = Depends(get_session)):
    return await _call(service.configure(db, payload))


@router.post("/refresh", status_code=202)
async def refresh(payload: CollectionConfirmation, db: AsyncSession = Depends(get_session)):
    return await _call(service.refresh(db, payload))


@router.get("/inbox")
async def inbox(shop_code: ShopCode, limit: Annotated[int, Query(ge=1, le=100)] = 50,
                offset: Annotated[int, Query(ge=0, le=1000000)] = 0, db: AsyncSession = Depends(get_session)):
    return await _call(service.inbox(db, shop_code, limit, offset))


@router.get("/runs")
async def runs(shop_code: ShopCode, db: AsyncSession = Depends(get_session)):
    return await _call(service.runs(db, shop_code))


@router.post("/stock-preview")
async def stock_preview(payload: StockProjectionRequest, db: AsyncSession = Depends(get_session)):
    return await _call(stock_projection.preview(db, payload.shop_code, payload.skus))
