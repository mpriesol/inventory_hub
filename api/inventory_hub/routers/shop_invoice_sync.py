"""Download existing shop documents; never issue or send an invoice."""
from fastapi import APIRouter, Depends, HTTPException, Path, Request
from sqlalchemy.ext.asyncio import AsyncSession
from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.routers.fifo import NoStoreRoute
from inventory_hub.shop_invoice_types import ShopInvoiceSyncRequest
from inventory_hub.services import shop_invoice_sync
from inventory_hub.services.shop_invoice_source import SourceError

router = APIRouter(prefix='/invoices/sync', tags=['shop-invoice-sync'], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])

@router.post('/shop/{shop_code}')
async def refresh(payload: ShopInvoiceSyncRequest, request: Request,
                  shop_code: str = Path(pattern=r'^[A-Za-z0-9_-]{1,50}$'),
                  db: AsyncSession = Depends(get_session)):
    principal = getattr(request.state, 'hub_user', None)
    actor = principal.get('username', 'operator') if principal else 'operator-token'
    try:
        return await shop_invoice_sync.refresh(db, shop_code, payload, actor=actor)
    except SourceError as error:
        headers = {'Retry-After': str(error.retry_after)} if error.retry_after is not None else None
        raise HTTPException(error.status, detail={'code': error.code, **error.diagnostics()}, headers=headers) from None
