"""Private supplier preorder entry and readback."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.routers.product_editor import NoStoreRoute
from inventory_hub.services import product_editor, supplier_preorders as service


router = APIRouter(prefix="/supplier-preorders", tags=["supplier-preorders"], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])


def actor(request):
    user = getattr(request.state, "hub_user", None)
    return f"user:{user['id']}" if user and user.get("id") is not None else "operator-token"


async def call(db, operation, *, write=False):
    try:
        result = await operation
        if write:
            await db.commit()
        return result
    except (service.PreorderError, product_editor.EditorError) as error:
        await db.rollback()
        raise HTTPException(error.status, detail={"code": error.code}) from None


@router.get("")
async def list_preorders(product_id: int | None = Query(None, gt=0), q: str = Query("", max_length=100),
                        page: int = Query(1, ge=1, le=1000), db: AsyncSession = Depends(get_session)):
    return await call(db, service.list_orders(db, product_id=product_id, query=q.strip(), page=page))


@router.get("/suppliers")
async def suppliers(db: AsyncSession = Depends(get_session)):
    return [dict(row) for row in (await db.execute(text(
        "SELECT id,name FROM suppliers WHERE is_active ORDER BY name,id"))).mappings().all()]


@router.post("")
async def create(payload: service.PreorderCreate, request: Request, db: AsyncSession = Depends(get_session)):
    return await call(db, service.create(db, payload, actor=actor(request)), write=True)


@router.get("/{identifier}")
async def detail(identifier: UUID, db: AsyncSession = Depends(get_session)):
    return await call(db, service.detail(db, identifier))


@router.get("/{identifier}/invoices")
async def invoices(identifier: UUID, q: str = Query("", max_length=100), db: AsyncSession = Depends(get_session)):
    return await call(db, service.available_invoices(db, identifier, q.strip()))


@router.post("/{identifier}/invoices")
async def link(identifier: UUID, payload: service.InvoiceLink, request: Request, db: AsyncSession = Depends(get_session)):
    return await call(db, service.link_invoice(db, identifier, payload.invoice_id, actor=actor(request)), write=True)


@router.put("/{identifier}/lines/{line_id}")
async def change_date(identifier: UUID, line_id: int, payload: service.ExpectedDate, db: AsyncSession = Depends(get_session)):
    return await call(db, service.change_date(db, identifier, line_id, payload), write=True)
