"""Authenticated maintenance operations; never echo submitted data in errors."""
from typing import Annotated
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession
from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.stock_publication_types import (
    StockPublicationConfigure, StockPublicationOpenHold, StockPublicationReleaseHold,
    StockPublicationPreview, StockPublicationSubmit, StockPublicationConfirmed, StockPublicationResolve,
)
from inventory_hub.services import stock_publication as service


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
                    "code": "stock_publication_invalid_request", "message": "stock_publication_invalid_request"}})
            response.headers["Cache-Control"] = "no-store"
            return response
        return handler


router = APIRouter(prefix="/stock-publication", tags=["stock-publication"], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])
ShopCode = Annotated[str, Query(pattern=r"^[a-z0-9][a-z0-9_-]{0,49}$")]


async def _call(operation):
    try:
        return await operation
    except service.PublicationError as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.code}) from None


@router.get("/options")
async def options(shop_code: ShopCode, db: AsyncSession = Depends(get_session)):
    return await _call(service.options(db, shop_code))


@router.post("/configure")
async def configure(payload: StockPublicationConfigure, db: AsyncSession = Depends(get_session)):
    return await _call(service.configure(db, payload))


@router.post("/holds")
async def open_hold(payload: StockPublicationOpenHold, db: AsyncSession = Depends(get_session)):
    return await _call(service.open_hold(db, payload))


@router.post("/holds/{hold_id}/release")
async def release_hold(hold_id: UUID, payload: StockPublicationReleaseHold, db: AsyncSession = Depends(get_session)):
    return await _call(service.release_hold(db, str(hold_id), payload))


@router.post("/preview")
async def preview(payload: StockPublicationPreview, db: AsyncSession = Depends(get_session)):
    return await _call(service.preview(db, payload))


@router.get("/batches")
async def batches(shop_code: ShopCode, limit: Annotated[int, Query(ge=1, le=100)] = 20,
                  offset: Annotated[int, Query(ge=0, le=100000)] = 0, db: AsyncSession = Depends(get_session)):
    return await _call(service.batches(db, shop_code, limit, offset))


@router.get("/batches/{batch_id}")
async def get_batch(batch_id: UUID, db: AsyncSession = Depends(get_session)):
    return await _call(service.get_batch(db, str(batch_id)))


@router.post("/batches/{batch_id}/submit")
async def submit(batch_id: UUID, payload: StockPublicationSubmit, db: AsyncSession = Depends(get_session)):
    return await _call(service.submit(db, str(batch_id), payload))


@router.post("/batches/{batch_id}/cancel")
async def cancel(batch_id: UUID, payload: StockPublicationConfirmed, db: AsyncSession = Depends(get_session)):
    return await _call(service.cancel(db, str(batch_id), payload))


@router.post("/batches/{batch_id}/verify")
async def verify(batch_id: UUID, payload: StockPublicationConfirmed, db: AsyncSession = Depends(get_session)):
    return await _call(service.verify(db, str(batch_id), payload))


@router.post("/batches/{batch_id}/resolve")
async def resolve(batch_id: UUID, payload: StockPublicationResolve, db: AsyncSession = Depends(get_session)):
    return await _call(service.resolve(db, str(batch_id), payload))
