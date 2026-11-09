"""Explicit local reconciliation of supplier documents into the shared archive."""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.invoice_archive_types import InvoiceReconcileInput
from inventory_hub.routers.stock_settings import NoStoreRoute
from inventory_hub.services import invoice_archive as service
from inventory_hub.services import invoice_source_archives as sources
from inventory_hub.invoice_source_archive_types import SourceArchivePreview, SourceArchiveApply


router = APIRouter(prefix='/invoices', tags=['invoice-archive'], route_class=NoStoreRoute,
                  dependencies=[Depends(operator_access)])


@router.post('/reconcile')
async def reconcile(payload: InvoiceReconcileInput, db: AsyncSession = Depends(get_session)):
    try:
        return await service.reconcile_supplier(db, payload)
    except service.InvoiceArchiveError as error:
        raise HTTPException(error.status, detail={'code': error.code}) from None


@router.get('/archive-status')
async def archive_status():
    from inventory_hub.services.invoice_archive_worker import status
    return status()


async def _source_call(operation):
    try:
        return await operation
    except service.InvoiceArchiveError as error:
        raise HTTPException(error.status, detail={'code': error.code, 'known_unapplied': True}) from None


@router.get('/source-archives')
async def source_archives(supplier_code: str = Query(pattern=r'^[a-zA-Z0-9_-]{1,50}$'),
                          db: AsyncSession = Depends(get_session)):
    return await _source_call(sources.current(db, supplier_code))


@router.post('/source-archives/preview')
async def source_archive_preview(payload: SourceArchivePreview, db: AsyncSession = Depends(get_session)):
    return await _source_call(sources.preview(db, payload))


@router.post('/source-archives/apply')
async def source_archive_apply(payload: SourceArchiveApply, request: Request,
                               db: AsyncSession = Depends(get_session)):
    user = getattr(request.state, 'hub_user', None)
    actor = str(user.get('username') or user.get('id')) if user else 'operator-token'
    return await _source_call(sources.apply(db, payload, actor))


@router.get('/source-archives/operations/{identifier}')
async def source_archive_result(identifier: UUID, db: AsyncSession = Depends(get_session)):
    return await _source_call(sources.get(db, identifier))
