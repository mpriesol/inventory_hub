"""Explicit central authority and bounded, inheritable sync controls."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator
from inventory_hub.stock_publication_types import StockPublicationConfirmed, StockPublicationResolve, StockPublicationPreview


class WarehouseSyncInput(StockPublicationConfirmed):
    warehouse_code: StrictStr = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,49}$")
    expected_revision: StrictInt = Field(ge=0)
    interval_seconds: StrictInt = Field(ge=60, le=86400)
    batch_size: StrictInt = Field(ge=1, le=100)
    max_order_age_seconds: StrictInt = Field(ge=60, le=86400)


class ShopSyncInput(StockPublicationConfirmed):
    shop_code: StrictStr = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,49}$")
    expected_revision: StrictInt = Field(ge=0)
    enabled: StrictBool
    authorized: StrictBool
    interval_seconds: StrictInt | None = Field(default=None, ge=60, le=86400)
    batch_size: StrictInt | None = Field(default=None, ge=1, le=100)
    max_order_age_seconds: StrictInt | None = Field(default=None, ge=60, le=86400)
    hub_is_stock_authority: StrictBool = False
    external_stock_writers_disabled: StrictBool = False
    orders_reconciled: StrictBool = False


class SyncRunInput(StockPublicationConfirmed):
    shop_code: StrictStr = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,49}$")
    skus: list[StrictStr] | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("skus")
    @classmethod
    def validate_skus(cls, value):
        return StockPublicationPreview.exact_skus(value) if value is not None else None


SyncResolveInput = StockPublicationResolve
