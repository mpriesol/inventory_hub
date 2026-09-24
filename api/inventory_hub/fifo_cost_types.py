"""Strict operator inputs; old orders are selected individually, never bulk-enabled."""
from typing import Literal
from uuid import UUID
from pydantic import Field, StrictBool, StrictInt, StrictStr, field_validator
from inventory_hub.stock_publication_types import StockPublicationConfirmed


class CostWarehouseInput(StockPublicationConfirmed):
    warehouse_code: StrictStr = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,49}$")
    expected_revision: StrictInt = Field(ge=0)
    interval_seconds: StrictInt = Field(ge=60, le=86400)
    batch_size: StrictInt = Field(ge=1, le=100)


class CostShopInput(StockPublicationConfirmed):
    shop_code: StrictStr = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,49}$")
    warehouse_code: StrictStr = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,49}$")
    expected_revision: StrictInt = Field(ge=0)
    enabled: StrictBool
    product_cost_enabled: StrictBool
    order_cost_enabled: StrictBool
    interval_seconds: StrictInt | None = Field(default=None, ge=60, le=86400)
    batch_size: StrictInt | None = Field(default=None, ge=1, le=100)


class CostRunInput(StockPublicationConfirmed):
    shop_code: StrictStr = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,49}$")


class CostOrderPreviewInput(CostRunInput):
    order_number: StrictStr = Field(min_length=1, max_length=100)
    request_id: UUID | None = None

    @field_validator("order_number")
    @classmethod
    def exact_number(cls, value):
        if value.strip() != value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("An exact order number is required")
        return value


class CostResolveInput(StockPublicationConfirmed):
    original_request_settled: Literal[True]
    note: StrictStr = Field(min_length=5, max_length=500)

    @field_validator("original_request_settled", mode="before")
    @classmethod
    def explicit_settlement(cls, value):
        return cls.explicit_confirmation(value)

    @field_validator("note")
    @classmethod
    def meaningful_note(cls, value):
        if len(value.strip()) < 5 or any(ord(c) < 32 for c in value):
            raise ValueError("Record how the original request was confirmed finished")
        return value.strip()
