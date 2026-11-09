"""Explicit reversible retirement of an exact supplier document source."""
from typing import Literal
from uuid import UUID
from pydantic import Field, StrictStr, field_validator
from inventory_hub.invoice_archive_types import ArchiveInput


class SourceArchivePreview(ArchiveInput):
    request_id: UUID
    supplier_code: StrictStr = Field(pattern=r'^[a-zA-Z0-9_-]{1,50}$')
    source_key: StrictStr = Field(min_length=1, max_length=500)
    action: Literal['archive', 'restore']
    note: StrictStr = Field(min_length=1, max_length=2000)

    @field_validator('source_key', 'note')
    @classmethod
    def nonblank(cls, value):
        value = value.strip()
        if not value or any(ord(char) < 32 and char not in '\n\t' for char in value):
            raise ValueError('A nonblank explanation and exact source key are required')
        return value


class SourceArchiveApply(SourceArchivePreview):
    preview_hash: StrictStr = Field(pattern=r'^[a-f0-9]{64}$')
    confirmed: Literal[True]

    @field_validator('confirmed', mode='before')
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError('Explicit confirmation required')
        return value
