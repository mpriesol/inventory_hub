"""Explicit selections for the existing-card Atomer content pilot."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator


ContentField = Literal["long_description", "seo_title", "seo_description"]


class AtomerContentItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    hub_sku: StrictStr = Field(min_length=1, max_length=255)
    source_code: StrictStr = Field(min_length=1, max_length=255)
    fields: list[ContentField] = Field(min_length=1, max_length=3)

    @field_validator("hub_sku", "source_code")
    @classmethod
    def valid_code(cls, value):
        if value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("A nonblank, unmodified code is required")
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise ValueError("A UTF-8 code is required") from None
        return value

    @field_validator("fields")
    @classmethod
    def unique_fields(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Fields must be unique")
        return value


class AtomerContentBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AtomerContentItem] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def unique_selection(self):
        if len({row.request_id for row in self.items}) != len(self.items):
            raise ValueError("Request IDs must be unique")
        if len({row.source_code for row in self.items}) != len(self.items):
            raise ValueError("Source selections must be unique")
        return self


class AtomerRollbackRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
