"""Documented inventory cutover; quantities and acquisition prices are exact strings."""
from datetime import datetime
from decimal import Decimal
import re
from typing import Literal
from uuid import UUID
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator


class FifoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FifoCutoverLayer(FifoInput):
    quantity: StrictStr
    unit_cost: StrictStr | None = None
    cost_status: Literal["known", "provisional", "unknown"]
    physical_received_at: AwareDatetime
    source_reference: StrictStr = Field(min_length=1, max_length=255)

    @field_validator("quantity")
    @classmethod
    def quantity_value(cls, value):
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,3})?", value) or not Decimal("0") < Decimal(value) <= Decimal("999999999.999"):
            raise ValueError("Use a positive exact quantity with at most three decimals")
        return value

    @field_validator("unit_cost")
    @classmethod
    def cost_value(cls, value):
        if value is not None and (not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,4})?", value)
                                  or Decimal(value) > Decimal("99999999.9999")):
            raise ValueError("Use a nonnegative EUR price excluding VAT with at most four decimals")
        return value

    @field_validator("source_reference")
    @classmethod
    def documented_reference(cls, value):
        if value != value.strip() or not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("A nonblank document reference without control characters is required")
        value.encode("utf-8")
        return value

    @model_validator(mode="after")
    def cost_classification(self):
        if (self.cost_status == "unknown") != (self.unit_cost is None):
            raise ValueError("Unknown cost must be NULL; known/provisional cost needs a documented price")
        return self


class FifoCutoverPreview(FifoInput):
    request_id: UUID
    sku: StrictStr = Field(min_length=1, max_length=100)
    warehouse_code: StrictStr = Field(min_length=1, max_length=50)
    source_reference: StrictStr = Field(min_length=1, max_length=255)
    operator_name: StrictStr = Field(min_length=1, max_length=100)
    counted_at: AwareDatetime
    layers: list[FifoCutoverLayer] = Field(max_length=500)

    @field_validator("sku", "warehouse_code", "source_reference", "operator_name")
    @classmethod
    def exact_text(cls, value):
        if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Use exact nonblank text")
        value.encode("utf-8")
        return value


class FifoCutoverApply(FifoInput):
    preview_hash: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: Literal[True]
    quantities_verified: Literal[True]
    costs_documented: Literal[True]

    @field_validator("confirmed", "quantities_verified", "costs_documented", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation is required")
        return value


class FifoReceiptPreview(FifoCutoverLayer):
    request_id: UUID
    sku: StrictStr = Field(min_length=1, max_length=100)
    warehouse_code: StrictStr = Field(min_length=1, max_length=50)
    operator_name: StrictStr = Field(min_length=1, max_length=100)

    @field_validator("sku", "warehouse_code", "operator_name", "source_reference")
    @classmethod
    def exact_text(cls, value):
        return FifoCutoverPreview.exact_text(value)


class FifoReceiptApply(FifoCutoverApply):
    pass
