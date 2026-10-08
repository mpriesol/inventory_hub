"""Typed cells for durable, Hub-first supplier import drafts."""
from decimal import Decimal
from typing import Literal
from uuid import UUID
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from inventory_hub.catalog_types import CatalogParameter, ShopImportOptions
from inventory_hub.product_editor_types import decimal_text, safe_text, VariantPatch


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImportValues(StrictModel):
    code: str = Field(min_length=1, max_length=100)
    supplier_code: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=500)
    group_name: str = Field(default="", max_length=500)
    brand: str = Field(default="", max_length=100)
    manufacturer_code: str = Field(default="", max_length=100)
    eans: list[str] = Field(default_factory=list, max_length=20)
    images: list[str] = Field(default_factory=list, max_length=100)
    description_html: str = Field(default="", max_length=100000)
    short_description: str = Field(default="", max_length=5000)
    seo_title: str = Field(default="", max_length=500)
    seo_description: str = Field(default="", max_length=5000)
    seo_url: str = Field(default="", max_length=500)
    category_code: str | None = Field(default=None, max_length=100)
    parameters: list[CatalogParameter] = Field(default_factory=list, max_length=500)
    variant_attributes: list[CatalogParameter] = Field(default_factory=list, max_length=100)
    metadata: dict[str, str] = Field(default_factory=dict, max_length=100)
    purchase_net: str | None = None
    retail_gross: str | None = None
    sale_gross: str | None = None
    vat_percent: str | None = None
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    availability: str = Field(default="", max_length=100)
    ai_enabled: StrictBool = False
    ai_category_profile: str = Field(default="auto", pattern=r"^[a-zA-Z0-9_-]{1,80}$")

    @field_validator("purchase_net", "retail_gross", "sale_gross")
    @classmethod
    def money(cls, value):
        return decimal_text(value, maximum=Decimal("9999999999.99"), places=2)

    @field_validator("vat_percent")
    @classmethod
    def vat(cls, value):
        return decimal_text(value, maximum=Decimal("100"), places=2)

    @field_validator("eans")
    @classmethod
    def barcodes(cls, value):
        return VariantPatch.eans_valid(value)

    @field_validator("code", "supplier_code")
    @classmethod
    def codes(cls, value):
        return VariantPatch.sku_valid(value)

    @field_validator("name")
    @classmethod
    def product_name(cls, value):
        return safe_text(value, maximum=500, blank=False)

    @field_validator("parameters", "variant_attributes")
    @classmethod
    def attributes(cls, value):
        for item in value:
            safe_text(item.name, maximum=100, blank=False)
            safe_text(item.value, maximum=255, blank=False)
        return value

    @field_validator("variant_attributes")
    @classmethod
    def variant_identity(cls, value):
        if len({item.name.casefold() for item in value}) != len(value):
            raise ValueError("Duplicate variant attribute")
        return value

    @field_validator("images")
    @classmethod
    def image_urls(cls, value):
        for url in value:
            parsed = urlsplit(url)
            if len(url) > 2000 or parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("An HTTP(S) image URL without credentials is required")
        return list(dict.fromkeys(value))

    @field_validator("metadata")
    @classmethod
    def metas(cls, value):
        import re
        for key, content in value.items():
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", key) or key == "validation_required":
                raise ValueError("Invalid or reserved custom field")
            safe_text(content, maximum=5000)
        return value

    @field_validator("name", "group_name", "brand", "manufacturer_code", "description_html", "short_description", "seo_title", "seo_description", "seo_url", "availability")
    @classmethod
    def text(cls, value):
        return safe_text(value, maximum=100000)


class DraftCreate(StrictModel):
    request_id: UUID
    supplier: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,50}$")
    feed_key: str = Field(default="products", pattern=r"^[a-zA-Z0-9_-]{1,50}$")
    run_id: int | None = None
    product_ids: list[int] = Field(min_length=1, max_length=500)
    shop: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,50}$")
    options: ShopImportOptions = Field(default_factory=ShopImportOptions)


class DraftRevision(StrictModel):
    expected_revision: int = Field(ge=1)


class DraftRowPatch(StrictModel):
    id: int = Field(gt=0)
    values: dict = Field(min_length=1)


class DraftPatch(DraftRevision):
    rows: list[DraftRowPatch] = Field(min_length=1, max_length=500)


class DraftAiRequest(DraftRevision):
    research: Literal["feed_only", "official"] = "official"


class DraftPublish(DraftRevision):
    preview_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    retry_failed: StrictBool = False
