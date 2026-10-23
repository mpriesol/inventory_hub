"""Explicit local reconciliation of supplier documents into the shared archive."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.invoice_archive_types import InvoiceReconcileInput
from inventory_hub.routers.stock_settings import NoStoreRoute
from inventory_hub.services import invoice_archive as service


router = APIRouter(prefix='/invoices', tags=['invoice-archive'], route_class=NoStoreRoute,
                  dependencies=[Depends(operator_access)])


@router.post('/reconcile')
async def reconcile(payload: InvoiceReconcileInput, db: AsyncSession = Depends(get_session)):
    try:
        return await service.reconcile_supplier(db, payload)
    except service.InvoiceArchiveError as error:
        raise HTTPException(error.status, detail={'code': error.code}) from None
