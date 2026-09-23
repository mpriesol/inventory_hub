"""Explicit operator input for a frozen, pieces-only opening stock batch."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictStr, field_validator


class OpeningPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    warehouse_code: StrictStr = Field(min_length=1, max_length=50)
    source_reference: StrictStr = Field(min_length=1, max_length=255)
    operator_name: StrictStr = Field(min_length=1, max_length=100)
    counted_at: AwareDatetime
    csv_text: StrictStr

    @field_validator("warehouse_code", "source_reference", "operator_name")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        value = value.strip()
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise ValueError("Valid UTF-8 text is required") from None
        if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("A nonblank value without control characters is required")
        return value

    @field_validator("counted_at", mode="before")
    @classmethod
    def iso_datetime(cls, value):
        if isinstance(value, datetime):
            return value
        if not isinstance(value, str) or "T" not in value:
            raise ValueError("Use an ISO datetime with a timezone")
        return datetime.fromisoformat(value.replace("Z", "+00:00"))


class OpeningFinalizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_hash: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: Literal[True]
    receipts_reconciled: Literal[True]

    @field_validator("confirmed", "receipts_reconciled", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation is required")
        return value
