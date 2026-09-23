"""Protected, explicit order sample reads; no background import or stock writes."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.services.order_audit import OrderAuditError, audit_orders


router = APIRouter(prefix="/shops", tags=["order-audit"], dependencies=[Depends(operator_access)])


@router.get("/{shop_code}/upgates/orders/audit")
async def order_audit(
    response: Response,
    shop_code: Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9_-]{0,49}$")],
    days: Literal["7", "30", "90"] = "30",
    page: Annotated[int, Query(ge=1, le=1000)] = 1,
    db: AsyncSession = Depends(get_session),
):
    response.headers["Cache-Control"] = "no-store"
    # Auth runs first; enforce the no-mutation contract in PostgreSQL too.
    await db.execute(text("SET TRANSACTION READ ONLY"))
    try:
        return await audit_orders(db, shop_code, int(days), page)
    except OrderAuditError as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.code}) from None
