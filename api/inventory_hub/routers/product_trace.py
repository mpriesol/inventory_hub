"""Authenticated read-only exact EAN/SKU investigation."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.routers.fifo import NoStoreRoute
from inventory_hub.services.product_trace import trace

router = APIRouter(prefix='/product-trace', tags=['product-trace'], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])

@router.get('')
async def lookup(code: str = Query('', max_length=100), product_id: int | None = Query(None, ge=1),
                 invoice_page: int = Query(1, ge=1, le=100000), movement_page: int = Query(1, ge=1, le=100000),
                 db: AsyncSession = Depends(get_session)):
    return await trace(db, code=code, product_id=product_id, invoice_page=invoice_page, movement_page=movement_page)
