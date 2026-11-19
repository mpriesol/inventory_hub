"""Shared settings dictionaries and generic manual availability intent."""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.routers.product_editor import NoStoreRoute
from inventory_hub.services import availability_catalog as service
from inventory_hub.setting_catalog_models import SettingCatalogRequest
from inventory_hub.setting_catalog_types import AvailabilityChange, WarehouseChange, AvailabilityOverrideInput

router = APIRouter(prefix='/settings/catalogs', tags=['settings'], route_class=NoStoreRoute,
    dependencies=[Depends(operator_access)])


def actor(request):
    user = getattr(request.state, 'hub_user', None)
    return f"user:{user['id']}" if user and user.get('id') is not None else 'operator-token'


async def call(operation):
    try:
        return await operation
    except service.CatalogError as error:
        raise HTTPException(error.status, detail={'code': error.code, 'message': error.code}) from None


@router.get('/availabilities')
async def availabilities(include_inactive: bool = False, db: AsyncSession = Depends(get_session)):
    return await call(service.list_values(db, include_inactive))


@router.post('/availabilities')
async def save_availability(payload: AvailabilityChange, request: Request, db: AsyncSession = Depends(get_session)):
    return await call(service.save_value(db, payload, actor=actor(request)))


@router.get('/warehouses')
async def warehouses(include_inactive: bool = False, db: AsyncSession = Depends(get_session)):
    return await call(service.list_warehouses(db, include_inactive))


@router.post('/warehouses')
async def save_warehouse(payload: WarehouseChange, request: Request, db: AsyncSession = Depends(get_session)):
    return await call(service.save_warehouse(db, payload, actor=actor(request)))


@router.get('/requests/{request_id}')
async def result(request_id: UUID, db: AsyncSession = Depends(get_session)):
    row = await db.get(SettingCatalogRequest, str(request_id))
    if row is None:
        raise HTTPException(404, detail={'code': 'catalog_request_missing'})
    return row.result


@router.post('/product-availability')
async def override(payload: AvailabilityOverrideInput, request: Request, db: AsyncSession = Depends(get_session)):
    result = await call(service.apply_product_overrides(db,
        [change.model_dump(exclude_unset=True) for change in payload.changes], str(payload.request_id), actor(request)))
    await db.commit()
    return result
