"""Explicit preview and confirmation of a receipt correction or archive action."""
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator


class ReceivingOperationPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    action: Literal["reverse", "reopen", "archive", "restore"]
    reason: StrictStr = Field(min_length=3, max_length=1000)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value):
        if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Use a nonblank reason without control characters")
        return value


class ReceivingOperationApply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_hash: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation is required")
        return value
