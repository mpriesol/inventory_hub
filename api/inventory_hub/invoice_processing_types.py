"""Reviewed invoice-only actions; reasons and exact replay identities are required."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator


InvoiceProcessingAction = Literal['process_without_stock', 'reopen', 'archive', 'restore']


class InvoiceProcessingPreview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: InvoiceProcessingAction
    expected_revision: StrictInt = Field(ge=0)
    note: StrictStr = Field(min_length=1, max_length=2000)

    @field_validator('note')
    @classmethod
    def required_reason(cls, value):
        value = value.strip()
        if not value:
            raise ValueError('A reason is required')
        return value


class InvoiceProcessingApply(InvoiceProcessingPreview):
    request_id: UUID
    preview_hash: StrictStr = Field(pattern=r'^[0-9a-f]{64}$')
    confirmed: Literal[True]

    @field_validator('confirmed', mode='before')
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError('Explicit confirmation required')
        return value
