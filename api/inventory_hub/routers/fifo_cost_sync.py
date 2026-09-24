"""Operator-only configuration, preview and durable publication recovery."""
from typing import Annotated
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.routers.stock_publication import NoStoreRoute
from inventory_hub.fifo_cost_types import CostShopInput, CostWarehouseInput, CostRunInput, CostOrderPreviewInput, CostResolveInput
from inventory_hub.stock_publication_types import StockPublicationConfirmed
from inventory_hub.services import fifo_cost_sync as service

router = APIRouter(prefix='/fifo-cost-sync', tags=['fifo-cost-sync'], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])
ShopCode = Annotated[str, Query(pattern=r'^[a-z0-9][a-z0-9_-]{0,49}$')]


async def call(operation):
    try:
        return await operation
    except (service.CostSyncError, service.source.SourceError) as error:
        raise HTTPException(error.status, detail={'code': error.code, 'message': error.code}) from None


@router.get('/options')
async def options(shop_code: ShopCode, db: AsyncSession = Depends(get_session)):
    return await call(service.options(db, shop_code))


@router.post('/configure')
async def configure(payload: CostShopInput, db: AsyncSession = Depends(get_session)):
    return await call(service.configure(db, payload))


@router.post('/warehouse')
async def warehouse(payload: CostWarehouseInput, db: AsyncSession = Depends(get_session)):
    return await call(service.configure_warehouse(db, payload))


@router.post('/run')
async def run(payload: CostRunInput, db: AsyncSession = Depends(get_session)):
    return await call(service.enqueue(db, payload))


@router.get('/history')
async def history(shop_code: ShopCode, offset: Annotated[int, Query(ge=0)] = 0,
                  limit: Annotated[int, Query(ge=1, le=100)] = 50, db: AsyncSession = Depends(get_session)):
    return await call(service.history(db, shop_code, offset, limit))


@router.post('/orders/preview')
async def preview(payload: CostOrderPreviewInput, db: AsyncSession = Depends(get_session)):
    return await call(service.order_preview(db, payload))


@router.get('/publications/{publication_id}')
async def get_publication(publication_id: UUID, db: AsyncSession = Depends(get_session)):
    return await call(service.get_publication(db, str(publication_id)))


@router.post('/publications/{publication_id}/send')
async def send(publication_id: UUID, payload: StockPublicationConfirmed, db: AsyncSession = Depends(get_session)):
    return await call(service.send(db, str(publication_id)))


@router.post('/publications/{publication_id}/resolve')
async def resolve(publication_id: UUID, payload: CostResolveInput, db: AsyncSession = Depends(get_session)):
    return await call(service.resolve(db, str(publication_id), payload))
