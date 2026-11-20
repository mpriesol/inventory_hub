"""Strict settings: scheduling never grants authority over physical stock."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


DEFAULT_FRESHNESS_SECONDS = 172800


class SupplierAvailabilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=0)
    enabled: bool = False
    feed_key: str = Field(default="products", pattern=r"^[a-zA-Z0-9_-]{1,50}$")
    interval_seconds: int = Field(default=86400, ge=300, le=604800)
    freshness_seconds: int = Field(default=DEFAULT_FRESHNESS_SECONDS, ge=300, le=2592000)
    min_coverage_percent: int = Field(default=100, ge=1, le=100)
    missing_visibility: Literal["keep", "archive"] | None = None
    missing_availability: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[^\x00-\x1f\x7f]+$")

    @model_validator(mode="after")
    def freshness_covers_interval(self):
        if self.freshness_seconds < self.interval_seconds:
            raise ValueError("Freshness must cover at least one collection interval")
        if self.missing_availability is not None and (
                not self.missing_availability.strip() or self.missing_availability != self.missing_availability.strip()):
            raise ValueError("Supply a nonblank availability label without leading or trailing spaces")
        return self


class SupplierAvailabilityRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=0)


class SupplierAvailabilityRecheckInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=0)
    product_ids: list[int] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def valid_products(self):
        if any(product_id <= 0 for product_id in self.product_ids) or len(set(self.product_ids)) != len(self.product_ids):
            raise ValueError("Select distinct positive product IDs")
        return self


class SupplierLinkReconcileInput(BaseModel):
    """A bounded local identity repair; never an upstream download or publication."""
    model_config = ConfigDict(extra="forbid", strict=True)
    after_product_id: int = Field(default=0, ge=0)
    limit: int = Field(default=500, ge=1, le=500)


class VerifiedSupplierLinkInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    product_id: int = Field(gt=0)
    hub_sku: str = Field(min_length=1, max_length=100, pattern=r"^[^\x00-\x1f\x7f]+$")
    supplier_sku: str = Field(min_length=1, max_length=100, pattern=r"^[^\x00-\x1f\x7f]+$")
    ean: str = Field(pattern=r"^\d{8,14}$")

    @model_validator(mode="after")
    def exact_codes(self):
        if self.hub_sku.strip() != self.hub_sku or self.supplier_sku.strip() != self.supplier_sku:
            raise ValueError("Supply exact SKU values without leading or trailing spaces")
        return self


class VerifiedSupplierLinksInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirmed: Literal[True]
    items: list[VerifiedSupplierLinkInput] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_items(self):
        if (len({row.product_id for row in self.items}) != len(self.items)
                or len({row.supplier_sku for row in self.items}) != len(self.items)):
            raise ValueError("Select distinct cards and supplier codes")
        return self
