"""Explicit selections for the existing-card Atomer content pilot."""
from typing import Literal
import re
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator


ContentField = Literal["long_description", "seo_title", "seo_description", "parameters"]


class AtomerContentItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    hub_sku: StrictStr = Field(min_length=1, max_length=255)
    source_code: StrictStr = Field(min_length=1, max_length=255)
    fields: list[ContentField] = Field(min_length=1, max_length=4)

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
    parameter_names: dict[str, StrictStr] = Field(default_factory=dict, max_length=500)
    formatting: Literal['original', 'upgates'] = 'original'

    @field_validator('parameter_names')
    @classmethod
    def valid_parameter_names(cls, value):
        if any(not re.fullmatch(r'PROPERTY_\d+', key) or not name or name != name.strip()
               or len(name) > 255 or any(ord(char) < 32 for char in name) for key, name in value.items()):
            raise ValueError('Exact reviewed property names are required')
        return value

    @model_validator(mode="after")
    def unique_selection(self):
        if any('parameters' in row.fields for row in self.items) and not self.parameter_names:
            raise ValueError('Parameter name mapping is required')
        if len({row.request_id for row in self.items}) != len(self.items):
            raise ValueError("Request IDs must be unique")
        if len({row.source_code for row in self.items}) != len(self.items):
            raise ValueError("Source selections must be unique")
        return self


class AtomerRollbackRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
