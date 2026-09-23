"""Protected automatic-processing observations and explicit queue wake-up."""
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, StrictStr, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.routers.stock_settings import NoStoreRoute, ShopCode
from inventory_hub.services import order_processing as service
from inventory_hub.services.stock_settings import SettingsError


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
    except (service.ProcessingError, SettingsError) as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.code}) from None


@router.get("/jobs")
async def jobs(shop_code: ShopCode, limit: Annotated[int, Query(ge=1, le=100)] = 50,
               offset: Annotated[int, Query(ge=0, le=1000000)] = 0, db: AsyncSession = Depends(get_session)):
    return await _call(service.jobs(db, shop_code, limit, offset))


@router.post("/refresh", status_code=202)
async def refresh(payload: ProcessingRefresh, db: AsyncSession = Depends(get_session)):
    return await _call(service.refresh(db, payload.shop_code))
