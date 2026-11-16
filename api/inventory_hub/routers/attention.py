"""Authenticated shared incident API; all responses are private and non-cacheable."""
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession
from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.routers.stock_settings import NoStoreRoute
from inventory_hub.attention_types import AttentionEvent, AttentionAction, AttentionRuleInput
from inventory_hub.services import attention as service

router = APIRouter(prefix="/attention", tags=["attention"], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])
Kind = Annotated[str, Path(pattern=r"^[a-zA-Z0-9_.:-]{1,100}$")]
Scope = Annotated[str, Query(pattern=r"^(\*|[a-zA-Z0-9_.:-]{1,100})$")]


async def call(operation):
    try:
        return await operation
    except service.AttentionError as error:
        raise HTTPException(error.status, detail={"code": error.code}) from None


@router.get("")
async def overview(state: Literal["active", "ignored", "muted", "resolved"] = "active",
                   source: Annotated[str | None, Query(pattern=r"^[a-zA-Z0-9_.:-]{1,100}$")] = None,
                   limit: Annotated[int, Query(ge=1, le=100)] = 20,
                   offset: Annotated[int, Query(ge=0)] = 0, db: AsyncSession = Depends(get_session)):
    return await call(service.overview(db, state, source, limit, offset))


@router.get("/rules")
async def rules(db: AsyncSession = Depends(get_session)):
    return await call(service.rules(db))


@router.put("/rules/{kind}")
async def configure(kind: Kind, payload: AttentionRuleInput, scope: Scope = "*", db: AsyncSession = Depends(get_session)):
    return await call(service.set_rule(db, kind, scope, payload))


@router.post("/events")
async def report(payload: AttentionEvent, db: AsyncSession = Depends(get_session)):
    return await call(service.report(db, payload))


@router.post("/{item_id}/action")
async def action(item_id: Annotated[int, Path(gt=0)], payload: AttentionAction, db: AsyncSession = Depends(get_session)):
    return await call(service.action(db, item_id, payload))
