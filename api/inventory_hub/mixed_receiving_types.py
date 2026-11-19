from decimal import Decimal
from typing import Optional, Literal
from uuid import UUID
from pydantic import BaseModel, Field, ConfigDict, StrictBool, field_validator, model_validator
from inventory_hub.fifo_types import FifoCutoverLayer


class BatchInput(BaseModel):
    model_config = ConfigDict(extra='forbid')


class BatchCreate(BatchInput):
    request_id: UUID
    warehouse_code: Optional[str] = Field(default=None, max_length=50)


class BatchScan(BatchInput):
    request_id: UUID
    code: str = Field(min_length=1, max_length=100, pattern=r'\S')
    qty: Decimal = Field(default=Decimal('1'), gt=0, max_digits=12, decimal_places=3)
    supplier: Optional[str] = Field(default=None, min_length=1, max_length=100)
    invoice_no: Optional[str] = Field(default=None, min_length=1, max_length=100)
    line_id: Optional[int] = Field(default=None, gt=0)


class BatchAssign(BatchInput):
    request_id: UUID
    supplier: str = Field(min_length=1, max_length=100)
    invoice_no: str = Field(min_length=1, max_length=100)
    line_id: Optional[int] = Field(default=None, gt=0)


class BatchCancel(BatchInput):
    request_id: UUID


class BatchArchive(BatchInput):
    request_id: UUID
    reason: str = Field(min_length=3, max_length=255)
    empty_only: StrictBool = True

    @field_validator('reason')
    @classmethod
    def documented_reason(cls, value):
        return FifoCutoverLayer.documented_reference(value)


class BatchManualScan(BatchInput):
    request_id: UUID
    code: str = Field(min_length=1, max_length=100, pattern=r'\S')
    qty: Decimal = Field(default=Decimal('1'), gt=0, max_digits=12, decimal_places=3)
    confirmed_without_invoice: StrictBool
    reason: str = Field(min_length=3, max_length=255)
    unit_cost: str | None = None
    cost_status: Literal['known', 'provisional', 'unknown'] = 'unknown'

    @field_validator('confirmed_without_invoice')
    @classmethod
    def require_confirmation(cls, value):
        if not value:
            raise ValueError('Explicit receipt without invoice confirmation is required')
        return value

    @field_validator('reason')
    @classmethod
    def documented_reason(cls, value):
        return FifoCutoverLayer.documented_reference(value)

    @field_validator('unit_cost')
    @classmethod
    def documented_cost(cls, value):
        return FifoCutoverLayer.cost_value(value)

    @model_validator(mode='after')
    def cost_classification(self):
        if (self.cost_status == 'unknown') != (self.unit_cost is None):
            raise ValueError('Unknown cost must be NULL; known/provisional cost needs a documented price')
        return self


class BatchFinalize(BatchInput):
    request_id: UUID
    preview_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    confirmed: StrictBool

    @field_validator('confirmed')
    @classmethod
    def require_confirmation(cls, value):
        if not value:
            raise ValueError('Confirmation is required')
        return value
