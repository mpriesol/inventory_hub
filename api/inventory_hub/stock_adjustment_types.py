"""Documented physical recounts; derived reservations and values are never inputs."""
from decimal import Decimal
import re
from typing import Literal
from uuid import UUID
from pydantic import AwareDatetime, Field, StrictStr, field_validator, model_validator
from inventory_hub.fifo_types import FifoInput, FifoCutoverApply, FifoCutoverPreview, FifoCutoverLayer


class StockAdjustmentPreview(FifoInput):
    request_id: UUID
    sku: StrictStr = Field(min_length=1, max_length=100)
    warehouse_code: StrictStr = Field(min_length=1, max_length=50)
    counted_quantity: StrictStr
    counted_at: AwareDatetime
    source_reference: StrictStr = Field(min_length=1, max_length=255)
    operator_name: StrictStr = Field(min_length=1, max_length=100)
    reason: StrictStr = Field(min_length=1, max_length=500)
    unit_cost: StrictStr | None = None
    cost_status: Literal["known", "provisional", "unknown"] = "unknown"

    @field_validator("sku", "warehouse_code", "source_reference", "operator_name", "reason")
    @classmethod
    def exact_text(cls, value):
        return FifoCutoverPreview.exact_text(value)

    @field_validator("counted_quantity")
    @classmethod
    def whole_quantity(cls, value):
        if not re.fullmatch(r"[0-9]{1,9}", value) or Decimal(value) > Decimal("999999999"):
            raise ValueError("Use a nonnegative whole physical count")
        return value

    @field_validator("unit_cost")
    @classmethod
    def cost_value(cls, value):
        return FifoCutoverLayer.cost_value(value)

    @model_validator(mode="after")
    def classified_cost(self):
        if (self.cost_status == "unknown") != (self.unit_cost is None):
            raise ValueError("Unknown cost must be NULL; known/provisional cost requires a price")
        return self


class StockAdjustmentApply(FifoCutoverApply):
    pass
