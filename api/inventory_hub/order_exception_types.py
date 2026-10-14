"""Explicit acknowledgement of an already-issued order; no client stock facts."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator


class ResolutionPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID


class ResolutionConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_hash: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: Literal[True]
    stock_accounted: Literal[True]
    reason: StrictStr = Field(min_length=1, max_length=1000)

    @field_validator("confirmed", "stock_accounted", mode="before")
    @classmethod
    def actual_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation is required")
        return value

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value):
        value = value.strip()
        if not value or any((ord(char) < 32 and char not in "\n\t") or ord(char) == 127 for char in value):
            raise ValueError("A reason is required")
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise ValueError("Invalid reason") from None
        return value
