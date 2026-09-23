"""Only explicit operator assertions and frozen draft identities are accepted."""
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator


class PublicationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StockPublicationConfirmed(PublicationInput):
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation is required")
        return value


class StockPublicationConfigure(StockPublicationConfirmed):
    shop_code: StrictStr = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,49}$")
    expected_revision: int = Field(ge=0, strict=True)
    enabled: StrictBool


class StockPublicationOpenHold(StockPublicationConfirmed):
    shop_code: StrictStr = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,49}$")
    external_writers_paused: Literal[True]
    orders_reconciled: Literal[True]

    @field_validator("external_writers_paused", "orders_reconciled", mode="before")
    @classmethod
    def explicit_assertion(cls, value):
        return cls.explicit_confirmation(value)


class StockPublicationReleaseHold(StockPublicationConfirmed):
    maintenance_completed: Literal[True]

    @field_validator("maintenance_completed", mode="before")
    @classmethod
    def explicit_assertion(cls, value):
        return cls.explicit_confirmation(value)


class StockPublicationPreview(PublicationInput):
    request_id: UUID
    shop_code: StrictStr = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,49}$")
    skus: list[StrictStr] = Field(min_length=1, max_length=100)

    @field_validator("skus")
    @classmethod
    def exact_skus(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("Duplicate SKU")
        for value in values:
            try:
                value.encode("utf-8")
            except UnicodeError:
                raise ValueError("Invalid SKU") from None
            if not value or len(value) > 100 or value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError("Invalid SKU")
        return values


class StockPublicationSubmit(StockPublicationConfirmed):
    preview_hash: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")


class StockPublicationResolve(StockPublicationConfirmed):
    external_requests_finished: Literal[True]

    @field_validator("external_requests_finished", mode="before")
    @classmethod
    def explicit_assertion(cls, value):
        return cls.explicit_confirmation(value)
