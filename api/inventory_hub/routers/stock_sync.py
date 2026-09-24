"""Operator controls for recurring and manual stock/availability publication."""
from typing import Annotated
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession
from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.routers.stock_publication import NoStoreRoute
from inventory_hub.stock_sync_types import WarehouseSyncInput, ShopSyncInput, SyncRunInput, SyncResolveInput
from inventory_hub.services import stock_sync as service

router = APIRouter(prefix='/stock-sync', tags=['stock-sync'], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])
ShopCode = Annotated[str, Query(pattern=r'^[a-z0-9][a-z0-9_-]{0,49}$')]


async def call(operation):
    try:
        return await operation
    except service.SyncError as error:
        raise HTTPException(error.status, detail={'code': error.code, 'message': error.code}) from None


@router.get('/options')
async def options(shop_code: ShopCode, db: AsyncSession = Depends(get_session)):
    return await call(service.options(db, shop_code))


@router.post('/warehouse')
async def warehouse(payload: WarehouseSyncInput, db: AsyncSession = Depends(get_session)):
    return await call(service.configure_warehouse(db, payload))


@router.post('/configure')
async def configure(payload: ShopSyncInput, db: AsyncSession = Depends(get_session)):
    return await call(service.configure(db, payload))


@router.post('/run')
async def run(payload: SyncRunInput, db: AsyncSession = Depends(get_session)):
    return await call(service.enqueue(db, payload))


@router.get('/runs/{run_id}')
async def get_run(run_id: UUID, offset: Annotated[int, Query(ge=0)] = 0,
                  limit: Annotated[int, Query(ge=1, le=1000)] = 1000, db: AsyncSession = Depends(get_session)):
    return await call(service.get_run(db, str(run_id), offset, limit))


@router.post('/items/{item_id}/resolve')
async def resolve(item_id: Annotated[int, Path(ge=1)], payload: SyncResolveInput,
                  db: AsyncSession = Depends(get_session)):
    return await call(service.resolve(db, item_id, payload))
