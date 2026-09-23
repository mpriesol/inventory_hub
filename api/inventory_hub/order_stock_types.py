"""Operator inputs; order contents and stock quantities always come from the server."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator


class OrderStockInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrderStockPreviewRequest(OrderStockInput):
    request_id: UUID
    shop_code: StrictStr = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,49}$")
    order_number: StrictStr = Field(min_length=1, max_length=100)

    @field_validator("order_number")
    @classmethod
    def exact_order_number(cls, value):
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise ValueError("Invalid order number") from None
        if value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Invalid order number")
        return value


class OrderStockConfigureRequest(OrderStockInput):
    shop_code: StrictStr = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,49}$")
    warehouse_code: StrictStr = Field(min_length=1, max_length=50)
    status_hash: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    status_actions: dict[str, Literal["reserve", "issue", "cancel", "review"]] = Field(max_length=1000)
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation is required")
        return value


class OrderStockApplyRequest(OrderStockInput):
    preview_hash: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: Literal[True]
    physical_confirmed: StrictBool

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation is required")
        return value
