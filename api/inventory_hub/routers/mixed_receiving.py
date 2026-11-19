from uuid import UUID
from fastapi import APIRouter, Depends, Request, Query
from sqlalchemy.ext.asyncio import AsyncSession
from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.mixed_receiving_types import BatchCreate, BatchScan, BatchAssign, BatchCancel, BatchFinalize, BatchArchive, BatchManualScan
from inventory_hub.routers.product_editor import NoStoreRoute
from inventory_hub.services import mixed_receiving as service

router = APIRouter(prefix='/receiving/batches', tags=['Receiving'], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])


def actor(request):
    user = getattr(request.state, 'hub_user', None)
    return f"user:{user['id']}" if user and user.get('id') is not None else 'operator-token'


@router.get('')
async def list_batches(status: str = Query(default='open', pattern='^(open|completed)$'), db: AsyncSession = Depends(get_session)):
    return await service.list_batches(db, status)


@router.post('')
async def create(body: BatchCreate, request: Request, db: AsyncSession = Depends(get_session)):
    return await service.create(db, body, actor=actor(request))


@router.get('/{batch_id}')
async def summary(batch_id: UUID, db: AsyncSession = Depends(get_session)):
    return await service.summary(db, batch_id)


@router.post('/{batch_id}/scan')
async def scan(batch_id: UUID, body: BatchScan, request: Request, db: AsyncSession = Depends(get_session)):
    return await service.scan(db, batch_id, body, actor=actor(request))


@router.get('/{batch_id}/manual-product')
async def manual_product(batch_id: UUID, code: str = Query(min_length=1, max_length=100, pattern=r'\S'),
                         db: AsyncSession = Depends(get_session)):
    return await service.manual_product(db, batch_id, code)


@router.post('/{batch_id}/manual-scan')
async def manual_scan(batch_id: UUID, body: BatchManualScan, request: Request, db: AsyncSession = Depends(get_session)):
    return await service.manual_scan(db, batch_id, body, actor=actor(request))


@router.post('/{batch_id}/archive')
async def archive(batch_id: UUID, body: BatchArchive, request: Request, db: AsyncSession = Depends(get_session)):
    return await service.archive(db, batch_id, body, actor=actor(request))


@router.post('/{batch_id}/restore')
async def restore(batch_id: UUID, body: BatchArchive, request: Request, db: AsyncSession = Depends(get_session)):
    return await service.archive(db, batch_id, body, actor=actor(request), restore=True)


@router.post('/{batch_id}/scans/{scan_id}/reassign')
async def reassign(batch_id: UUID, scan_id: UUID, body: BatchAssign, request: Request, db: AsyncSession = Depends(get_session)):
    return await service.reassign(db, batch_id, scan_id, body, actor=actor(request))


@router.post('/{batch_id}/scans/{scan_id}/cancel')
async def cancel(batch_id: UUID, scan_id: UUID, body: BatchCancel, request: Request, db: AsyncSession = Depends(get_session)):
    return await service.cancel(db, batch_id, scan_id, body, actor=actor(request))


@router.post('/{batch_id}/preview')
async def preview(batch_id: UUID, db: AsyncSession = Depends(get_session)):
    return await service.preview(db, batch_id)


@router.post('/{batch_id}/finalize')
async def finalize(batch_id: UUID, body: BatchFinalize, request: Request, db: AsyncSession = Depends(get_session)):
    return await service.finalize(db, batch_id, body, actor=actor(request))
