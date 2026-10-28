"""Strict settings: scheduling never grants authority over physical stock."""
from pydantic import BaseModel, ConfigDict, Field, model_validator


class SupplierAvailabilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=0)
    enabled: bool = False
    feed_key: str = Field(default="products", pattern=r"^[a-zA-Z0-9_-]{1,50}$")
    interval_seconds: int = Field(default=86400, ge=300, le=604800)
    freshness_seconds: int = Field(default=172800, ge=300, le=2592000)
    min_coverage_percent: int = Field(default=100, ge=1, le=100)

    @model_validator(mode="after")
    def freshness_covers_interval(self):
        if self.freshness_seconds < self.interval_seconds:
            raise ValueError("Freshness must cover at least one collection interval")
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
