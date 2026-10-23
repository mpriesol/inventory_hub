"""Private read-only calendar of original invoice due dates."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.invoice_calendar_types import InvoiceCalendarQuery
from inventory_hub.routers.invoices_unified import InvoiceRoute
from inventory_hub.services import invoice_calendar as service


router = APIRouter(prefix="/invoices", tags=["Invoice calendar"], route_class=InvoiceRoute,
                   dependencies=[Depends(operator_access)])


@router.get("/calendar")
async def calendar(query: Annotated[InvoiceCalendarQuery, Query()], db: AsyncSession = Depends(get_session)):
    try:
        return await service.calendar(db, query)
    except service.InvoiceCalendarError as error:
        raise HTTPException(error.status, detail={"code": error.code}) from None
