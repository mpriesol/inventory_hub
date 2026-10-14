"""Protected automatic-processing observations and explicit queue wake-up."""
from typing import Annotated, Literal
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, StrictStr, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.routers.stock_settings import NoStoreRoute, ShopCode
from inventory_hub.services import order_processing as service
from inventory_hub.services.stock_settings import SettingsError
from inventory_hub.order_exception_types import ResolutionConfirmRequest, ResolutionPreviewRequest
from inventory_hub.services import order_exceptions
from inventory_hub.services.order_stock_source import SourceError


class ProcessingRefresh(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shop_code: StrictStr
    confirmed: Literal[True]

    @field_validator("shop_code")
    @classmethod
    def shop_identity(cls, value):
        import re
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,49}", value):
            raise ValueError("Invalid shop")
        return value

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation required")
        return value


router = APIRouter(prefix="/order-processing", tags=["order-processing"], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])


async def _call(operation):
    try:
        return await operation
    except (service.ProcessingError, SettingsError, order_exceptions.ExceptionError, SourceError) as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.code}) from None


@router.get("/jobs")
async def jobs(shop_code: ShopCode, limit: Annotated[int, Query(ge=1, le=100)] = 50,
               offset: Annotated[int, Query(ge=0, le=1000000)] = 0, db: AsyncSession = Depends(get_session)):
    return await _call(service.jobs(db, shop_code, limit, offset))


@router.post("/refresh", status_code=202)
async def refresh(payload: ProcessingRefresh, db: AsyncSession = Depends(get_session)):
    return await _call(service.refresh(db, payload.shop_code))


@router.post("/jobs/{job_id}/resolution-preview")
async def resolution_preview(job_id: int, payload: ResolutionPreviewRequest, db: AsyncSession = Depends(get_session)):
    return await _call(order_exceptions.preview(db, job_id, payload))


@router.get("/resolutions/{preview_id}")
async def resolution(preview_id: UUID, db: AsyncSession = Depends(get_session)):
    return await _call(order_exceptions.get_preview(db, str(preview_id)))


@router.post("/resolutions/{preview_id}/resolve")
async def resolve(preview_id: UUID, payload: ResolutionConfirmRequest, request: Request,
                  db: AsyncSession = Depends(get_session)):
    principal = getattr(request.state, "hub_user", None) or {}
    actor = f"user:{principal['id']}" if principal.get("id") is not None else "operator-token"
    return await _call(order_exceptions.resolve(db, str(preview_id), payload, actor))
