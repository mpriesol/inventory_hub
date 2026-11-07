"""Authenticated review of sale representations sharing a physical piece card."""
from uuid import UUID
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.routers.product_links import NoStoreRoute, actor, call
from inventory_hub.shared_stock_sale_types import SharedStockSaleApplyRequest, SharedStockSalePreviewRequest
from inventory_hub.services import shared_stock_sales as service


router = APIRouter(prefix='/shared-stock-sales', tags=['product-links'], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])


@router.post('/preview')
async def preview(payload: SharedStockSalePreviewRequest, request: Request, db: AsyncSession = Depends(get_session)):
    return await call(service.preview(db, payload, actor=actor(request)))


@router.get('/{identifier}')
async def get(identifier: UUID, db: AsyncSession = Depends(get_session)):
    return await call(service.get(db, str(identifier)))


@router.post('/{identifier}/apply')
async def apply(identifier: UUID, payload: SharedStockSaleApplyRequest, request: Request,
                db: AsyncSession = Depends(get_session)):
    return await call(service.apply(db, str(identifier), payload, actor=actor(request)))
