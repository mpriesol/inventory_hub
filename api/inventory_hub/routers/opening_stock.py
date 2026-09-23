"""Operator-only opening stock preview, recovery and explicit finalization."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.opening_stock_types import OpeningFinalizeRequest, OpeningPreviewRequest
from inventory_hub.services import opening_stock as service


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
                # Do not echo uploaded CSV or malformed Unicode into a response.
                response = JSONResponse(status_code=422, content={"detail": {
                    "code": "opening_invalid_request", "message": "opening_invalid_request",
                }})
            response.headers["Cache-Control"] = "no-store"
            return response

        return handler


router = APIRouter(prefix="/stock/opening", tags=["opening-stock"], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])


async def _call(operation):
    try:
        return await operation
    except service.OpeningError as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.code, "errors": error.errors}) from None


@router.get("/options")
async def options(db: AsyncSession = Depends(get_session)):
    return await _call(service.options(db))


@router.post("/preview")
async def preview(payload: OpeningPreviewRequest, db: AsyncSession = Depends(get_session)):
    return await _call(service.preview(db, payload))


@router.get("/batches")
async def list_batches(limit: Annotated[int, Query(ge=1, le=100)] = 20, db: AsyncSession = Depends(get_session)):
    return await _call(service.list_batches(db, limit))


@router.get("/{batch_id}")
async def get_batch(batch_id: UUID, db: AsyncSession = Depends(get_session)):
    return await _call(service.get_batch(db, str(batch_id)))


@router.post("/{batch_id}/finalize")
async def finalize(batch_id: UUID, payload: OpeningFinalizeRequest, db: AsyncSession = Depends(get_session)):
    return await _call(service.finalize(db, str(batch_id), payload))
