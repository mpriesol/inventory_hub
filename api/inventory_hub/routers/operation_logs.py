"""Protected, fixed-schema operational history. No arbitrary file reads."""
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from inventory_hub.access import operator_access
from inventory_hub import operation_log
from inventory_hub.routers.stock_settings import NoStoreRoute

router = APIRouter(prefix="/logs", tags=["logs"], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])
CODE = r"^[a-zA-Z0-9_.:-]{1,100}$"


@router.get("/events")
def events(limit: int = Query(100, ge=1, le=200),
           before: str | None = Query(None, pattern=r"^[a-f0-9]{32}$"),
           component: str | None = Query(None, pattern=CODE),
           supplier: str | None = Query(None, pattern=CODE),
           level: Literal["info", "warning", "error"] | None = None,
           run_id: int | None = Query(None, ge=1),
           request_id: str | None = Query(None, pattern=r"^[a-f0-9]{32}$"),
           since: datetime | None = None, until: datetime | None = None):
    if any(value is not None and value.tzinfo is None for value in (since, until)) or (since and until and since > until):
        raise HTTPException(422, detail={"code": "operation_log_invalid_range"})
    stamp = lambda value: value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ") if value else None
    try:
        return operation_log.read(limit=limit, before=before, component=component, supplier=supplier,
                                  level=level, run_id=run_id, request_id=request_id, since=stamp(since), until=stamp(until))
    except operation_log.CursorExpired:
        raise HTTPException(409, detail={"code": "operation_log_cursor_expired"}) from None
    except operation_log.LogUnavailable:
        raise HTTPException(503, detail={"code": "operation_log_unavailable"}) from None
