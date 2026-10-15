"""Explicit association of an existing card with an existing supplier offer."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator


class LinkInput(BaseModel):
    model_config = ConfigDict(extra='forbid')


class SupplierCardLinkCandidatesRequest(LinkInput):
    product_id: StrictInt = Field(gt=0)
    supplier_code: StrictStr = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,49}$')
    query: StrictStr = Field(default='', max_length=100)

    @field_validator('query')
    @classmethod
    def query_text(cls, value):
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError('Search text contains control characters')
        return value.strip()


class SupplierCardLinkPreviewRequest(LinkInput):
    request_id: UUID
    product_id: StrictInt = Field(gt=0)
    supplier_product_id: StrictInt = Field(gt=0)


class SupplierCardLinkApplyRequest(LinkInput):
    preview_hash: StrictStr = Field(pattern=r'^[0-9a-f]{64}$')
    confirmed: Literal[True]

    @field_validator('confirmed', mode='before')
    @classmethod
    def confirmation(cls, value):
        if value is not True:
            raise ValueError('Explicit confirmation is required')
        return value
