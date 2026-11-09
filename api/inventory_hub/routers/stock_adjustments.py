"""Protected physical-count corrections, preview first, no external writes."""
from uuid import UUID
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.routers.fifo import NoStoreRoute, _call
from inventory_hub.stock_adjustment_types import (
    StockAdjustmentApply, StockAdjustmentPreview,
    StockAdjustmentBatchApply, StockAdjustmentBatchPreview,
)
from inventory_hub.services import stock_adjustments as service

router = APIRouter(prefix="/stock-adjustments", tags=["stock-adjustments"],
    route_class=NoStoreRoute, dependencies=[Depends(operator_access)])


@router.post("/preview")
async def preview(payload: StockAdjustmentPreview, db: AsyncSession = Depends(get_session)):
    return await _call(service.preview(db, payload))


@router.post("/batch-preview")
async def preview_batch(payload: StockAdjustmentBatchPreview, db: AsyncSession = Depends(get_session)):
    return await _call(service.preview_batch(db, payload))


@router.post("/batch-apply")
async def apply_batch(payload: StockAdjustmentBatchApply, db: AsyncSession = Depends(get_session)):
    return await _call(service.apply_batch(db, payload))


@router.get("/{identifier}")
async def get(identifier: UUID, db: AsyncSession = Depends(get_session)):
    return await _call(service.get(db, identifier))


@router.post("/{identifier}/apply")
async def apply(identifier: UUID, payload: StockAdjustmentApply, db: AsyncSession = Depends(get_session)):
    return await _call(service.apply(db, identifier, payload))
