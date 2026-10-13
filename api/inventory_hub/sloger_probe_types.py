"""Write-only credentials and bounded, factual Sloger lookup results."""
from datetime import datetime
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class SlogerCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=200)
    password: SecretStr = Field(min_length=1, max_length=500)

    @field_validator("username")
    @classmethod
    def clean_username(cls, value):
        if value != value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("Use a nonempty username without surrounding whitespace or control characters")
        return value


class AvailabilityTier(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=200)
    quantity: str | None = Field(default=None, pattern=r"^\d{1,9}(?:\.\d{1,3})?$")
    quantity_kind: Literal["exact", "minimum", "unknown"] = "unknown"
    quantity_relation: Literal["equal", "greater_than", "at_least", "unknown"] = "unknown"
    lead_days: int | None = Field(default=None, ge=0, le=365)

    @field_validator("quantity")
    @classmethod
    def normalize_decimal(cls, value):
        return format(Decimal(value).normalize(), "f") if value is not None else None


class SlogerObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str = Field(min_length=1, max_length=100)
    product_name: str | None = Field(default=None, max_length=500)
    manufacturer_code: str | None = Field(default=None, max_length=100)
    eans: list[str] = Field(default_factory=list, max_length=20)
    purchase_price: str | None = Field(default=None, pattern=r"^\d{1,9}(?:\.\d{1,4})?$")
    retail_price: str | None = Field(default=None, pattern=r"^\d{1,9}(?:\.\d{1,4})?$")
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    price_basis: Literal["net", "gross", "unknown"] = "unknown"
    retail_price_basis: Literal["net", "gross", "unknown"] = "unknown"
    availability: str | None = Field(default=None, max_length=200)
    available: bool | None = None
    quantity: str | None = Field(default=None, pattern=r"^\d{1,9}(?:\.\d{1,3})?$")
    quantity_kind: Literal["exact", "minimum", "unknown"] = "unknown"
    quantity_relation: Literal["equal", "greater_than", "at_least", "unknown"] = "unknown"
    availability_tiers: list[AvailabilityTier] = Field(default_factory=list, max_length=20)
    source_url: str = Field(max_length=1000)
    observed_at: datetime

    @field_validator("purchase_price", "retail_price", "quantity")
    @classmethod
    def normalize_decimal(cls, value):
        return format(Decimal(value).normalize(), "f") if value is not None else None

    @field_validator("eans")
    @classmethod
    def bounded_eans(cls, values):
        if any(not value or len(value) > 32 or not value.isdigit() or set(value) == {"0"} for value in values):
            raise ValueError("EAN values must be actual source identifiers")
        return list(dict.fromkeys(values))

    @field_validator("source_url")
    @classmethod
    def public_source(cls, value):
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname not in ("sloger.sk", "www.sloger.sk")
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Use a public Sloger product source URL without credentials")
        return value

    @field_validator("observed_at")
    @classmethod
    def explicit_timezone(cls, value):
        if value.tzinfo is None:
            raise ValueError("Observation time must include its timezone")
        return value


class SlogerDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: Literal["login_page", "login_submit", "search", "product"]
    reason: Literal["http_status", "network", "redirect", "content_type", "challenge", "size", "deadline", "url", "structure", "authentication"]
    http_status: int | None = Field(default=None, ge=100, le=599)
    method: Literal["GET", "POST"] | None = None
    path_class: Literal["login", "shop_return", "b2b", "search", "product", "other"] | None = None


class SlogerLookupResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str = Field(min_length=1, max_length=100)
    observation: SlogerObservation | None = None
    error: Literal["sloger_b2b_unavailable", "sloger_auth_failed", "sloger_product_not_found",
                   "sloger_product_ambiguous", "sloger_b2b_invalid_evidence", "sloger_batch_aborted"] | None = None
    diagnostic: SlogerDiagnostic | None = None

    @model_validator(mode="after")
    def one_outcome(self):
        if (self.observation is None) == (self.error is None):
            raise ValueError("A supplier lookup needs exactly one observation or error")
        if self.observation is not None and self.observation.sku != self.sku:
            raise ValueError("The supplier observation must match the requested SKU")
        return self


class SlogerBatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[SlogerLookupResult] = Field(default_factory=list, max_length=10)
    aborted: bool = False

    @model_validator(mode="after")
    def unique_skus(self):
        if len({item.sku for item in self.items}) != len(self.items):
            raise ValueError("A supplier lookup batch must contain unique SKUs")
        return self
