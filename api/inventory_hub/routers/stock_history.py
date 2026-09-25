"""Protected local ledger reading; no Upgates calls and no mutation routes."""
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.db_models import MovementType
from inventory_hub.services import stock_history as service


class NoStoreRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handler(request):
            try:
                response = await original(request)
            except HTTPException as error:
                error.headers = {**(error.headers or {}), "Cache-Control": "no-store"}
                raise
            except RequestValidationError:
                response = JSONResponse(status_code=422, content={"detail": {"code": "stock_history_invalid_request"}})
            response.headers["Cache-Control"] = "no-store"
            return response
        return handler


router = APIRouter(prefix="/stock-history", tags=["stock-history"], route_class=NoStoreRoute, dependencies=[Depends(operator_access)])


@router.get("/options")
async def options(db: AsyncSession = Depends(get_session)):
    return await service.options(db)


@router.get("/movements")
async def movements(q: Annotated[str, Query(max_length=200)] = "",
    sku: Annotated[str | None, Query(max_length=100)] = None,
    warehouse_code: Annotated[str | None, Query(max_length=50)] = None,
    movement_type: MovementType | None = None, date_from: date | None = None, date_to: date | None = None,
    tracking_scope: Literal["all", "current", "historical"] = "all",
    page: Annotated[int, Query(ge=1, le=1000000)] = 1, page_size: Annotated[int, Query(ge=25, le=100)] = 50,
    snapshot_id: Annotated[int | None, Query(ge=0, le=9223372036854775807)] = None,
    db: AsyncSession = Depends(get_session)):
    if page_size not in (25, 50, 100):
        raise HTTPException(422, detail={"code": "stock_history_invalid_page_size"})
    if (date_from and date_to and date_to < date_from) or date_to == date.max:
        raise HTTPException(422, detail={"code": "stock_history_invalid_dates"})
    return await service.list_movements(db, q=q.strip(), sku=sku, warehouse_code=warehouse_code,
        movement_type=movement_type, date_from=date_from, date_to=date_to, page=page, page_size=page_size,
        snapshot_id=snapshot_id, tracking_scope=tracking_scope)
