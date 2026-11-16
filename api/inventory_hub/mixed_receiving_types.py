from decimal import Decimal
from typing import Optional, Literal
from uuid import UUID
from pydantic import BaseModel, Field, ConfigDict, StrictBool, field_validator


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
