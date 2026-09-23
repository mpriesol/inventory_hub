"""Only collection controls and local projection selections are accepted."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator


class CollectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shop_code: StrictStr = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,49}$")


class CollectionConfirmation(CollectionInput):
    expected_revision: StrictInt | None = Field(default=None, ge=1)
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation required")
        return value


class CollectionConfigure(CollectionConfirmation):
    enabled: StrictBool


class StockProjectionRequest(CollectionInput):
    skus: list[StrictStr] = Field(min_length=1, max_length=100)

    @field_validator("skus")
    @classmethod
    def exact_skus(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("Duplicate SKUs")
        for value in values:
            try:
                value.encode("utf-8")
            except UnicodeError:
                raise ValueError("Invalid SKU") from None
            if not value or len(value) > 100 or value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError("Invalid SKU")
        return values
