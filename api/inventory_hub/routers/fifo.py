"""Authenticated FIFO history, documented receipts and legacy cutover."""
from typing import Annotated
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.fifo_types import FifoCutoverApply, FifoCutoverPreview, FifoReceiptApply, FifoReceiptPreview
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute


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
                    "code": "fifo_invalid_request", "message": "fifo_invalid_request"}})
            response.headers["Cache-Control"] = "no-store"
            return response
        return handler

from inventory_hub.services import fifo as service

router = APIRouter(prefix="/fifo", tags=["fifo"], route_class=NoStoreRoute, dependencies=[Depends(operator_access)])


async def _call(operation):
    try:
        return await operation
    except service.FifoError as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.code}) from None


@router.get("/options")
async def options(db: AsyncSession = Depends(get_session)):
    return await _call(service.options(db))


@router.get("/stock")
async def stock(product_id: int, warehouse_code: str, limit: Annotated[int, Query(ge=1, le=500)] = 50,
                offset: Annotated[int, Query(ge=0)] = 0, db: AsyncSession = Depends(get_session)):
    return await _call(service.stock(db, product_id, warehouse_code, limit, offset))


@router.get("/history")
async def history(product_id: int, warehouse_code: str, limit: Annotated[int, Query(ge=1, le=500)] = 50,
                  offset: Annotated[int, Query(ge=0)] = 0, db: AsyncSession = Depends(get_session)):
    return await _call(service.history(db, product_id, warehouse_code, limit, offset))


@router.get("/movements/{movement_id}/allocations")
async def allocations(movement_id: int, db: AsyncSession = Depends(get_session)):
    return await _call(service.allocations(db, movement_id))


@router.post("/cutovers/preview")
async def cutover_preview(payload: FifoCutoverPreview, db: AsyncSession = Depends(get_session)):
    return await _call(service.cutover_preview(db, payload))


@router.get("/cutovers/{identifier}")
async def get_cutover(identifier: UUID, db: AsyncSession = Depends(get_session)):
    return await _call(service.get_cutover(db, str(identifier)))


@router.post("/cutovers/{identifier}/apply")
async def cutover_apply(identifier: UUID, payload: FifoCutoverApply, db: AsyncSession = Depends(get_session)):
    return await _call(service.cutover_apply(db, str(identifier), payload))


@router.post("/receipts/preview")
async def receipt_preview(payload: FifoReceiptPreview, db: AsyncSession = Depends(get_session)):
    return await _call(service.receipt_preview(db, payload))


@router.get("/receipts/{identifier}")
async def get_receipt(identifier: UUID, db: AsyncSession = Depends(get_session)):
    return await _call(service.get_receipt(db, str(identifier)))


@router.post("/receipts/{identifier}/apply")
async def receipt_apply(identifier: UUID, payload: FifoReceiptApply, db: AsyncSession = Depends(get_session)):
    return await _call(service.receipt_apply(db, str(identifier), payload))
