"""Public contracts for supplier catalogs and explicit shop imports."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CatalogParameter(BaseModel):
    name: str
    value: str


class CatalogPrices(BaseModel):
    currency: str
    vat_percent: Decimal | None = None
    vat_source: Literal["configured", "feed"] = "configured"
    purchase_net: Decimal | None = None
    purchase_gross: Decimal | None = None
    retail_net: Decimal | None = None
    retail_gross: Decimal | None = None
    discount_net: Decimal | None = None
    discount_gross: Decimal | None = None


class CatalogShopMatch(BaseModel):
    matched_by: Literal["code", "ean", "parent_code"]
    value: str
    code: str
    parent_code: str = ""
    remote_product_id: str | None = None


class CatalogProduct(BaseModel):
    id: int = 0
    supplier: str
    feed_key: str = "products"
    run_id: int | None = None
    code: str
    shop_code: str
    manufacturer_code: str | None = None
    eans: list[str] = Field(default_factory=list)
    name: str
    brand: str | None = None
    description: str = ""
    manufacturer_description: str = ""
    safety_information: str = ""
    category: str | None = None
    category_code: str | None = None
    url: str | None = None
    images: list[str] = Field(default_factory=list)
    parameters: list[CatalogParameter] = Field(default_factory=list)
    static_parameters: dict[str, Any] = Field(default_factory=dict)
    prices: CatalogPrices
    availability: str | None = None
    supplier_stock: Decimal | None = None
    supplier_stock_min: Decimal | None = None
    supplier_stock_raw: str | None = None
    supplier_stock_external: Decimal | None = None
    supplier_stock_external_raw: str | None = None
    supplier_external_available: bool | None = None
    delivery_date: str | None = None
    group_code: str | None = None
    group_name: str | None = None
    variant_attributes: list[CatalogParameter] = Field(default_factory=list)
    variant_relationship: Literal["explicit", "not_provided"] = "not_provided"
    warnings: list[str] = Field(default_factory=list)
    import_blockers: list[str] = Field(default_factory=list)
    fetched_at: datetime | None = None
    source_hash: str | None = None
    listed: bool = False
    shop_matches: list[CatalogShopMatch] = Field(default_factory=list)
    shop_url: str | None = None
    shop_admin_url: str | None = None
    shop_active: bool | None = None


class CatalogRow(BaseModel):
    """A standalone item or an explicit supplier group. IDs always identify items."""
    key: str
    product: CatalogProduct
    is_group: bool = False
    variants_count: int = 0
    matching_ids: list[int] = Field(default_factory=list)
    variants: list[CatalogProduct] = Field(default_factory=list)


class CatalogPage(BaseModel):
    supplier: str
    feed_key: str
    run_id: int | None = None
    fetched_at: datetime | None = None
    shop_checked_at: datetime | None = None
    total: int
    total_items: int
    page: int
    page_size: int
    pages: int
    manufacturers: list[str]
    items: list[CatalogRow]


class CatalogRefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feed_key: str = Field(default="products", pattern=r"^[a-zA-Z0-9_-]{1,50}$")


class CatalogSelection(BaseModel):
    supplier: str
    feed_key: str
    run_id: int | None
    ids: list[int]
    total: int


class ShopImportOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str = Field(default="sk", pattern=r"^[a-z]{2}(?:-[a-zA-Z]{2})?$")
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    pricelist: str = Field(default="Predvolené", min_length=1, max_length=100)
    category_code: str | None = Field(default=None, max_length=100)
    pricing: Literal["configured", "retail"] = "configured"
    include_images: bool = True
    include_description: bool = True
    include_parameters: bool = True


class ShopImportPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supplier: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,50}$")
    feed_key: str = Field(default="products", pattern=r"^[a-zA-Z0-9_-]{1,50}$")
    product_ids: list[int] = Field(min_length=1, max_length=20000)
    run_id: int | None = None
    options: ShopImportOptions = Field(default_factory=ShopImportOptions)
    refresh_shop: bool = False
    sale_price_overrides: dict[int, Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]] = Field(default_factory=dict, max_length=20000)

    @field_validator("product_ids")
    @classmethod
    def distinct_positive_ids(cls, ids: list[int]) -> list[int]:
        if any(i <= 0 for i in ids):
            raise ValueError("Product IDs must be positive")
        return list(dict.fromkeys(ids))


class ShopImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    retry_failed: bool = False


class ImportItem(BaseModel):
    code: str
    name: str
    product_ids: list[int]
    variants_count: int = 0
    existing_product_ids: list[int] = Field(default_factory=list)
    parent_exists: bool = False
    status: Literal["ready", "exists", "invalid", "created", "failed", "uncertain"]
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)


class ShopCheck(BaseModel):
    checked_at: datetime
    full_checked_at: datetime
    mode: Literal["full", "changes"]


class ImportPriceLine(BaseModel):
    product_id: int
    code: str
    name: str
    image: str | None = None
    attributes: list[CatalogParameter] = Field(default_factory=list)
    retail_gross: Decimal | None = None
    purchase_net: Decimal | None = None
    sale_gross: Decimal | None = None
    overridden: bool = False
    blocked: bool = False
    existing: bool = False
    shop_matches: list[CatalogShopMatch] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ShopImportPreview(BaseModel):
    preview_id: str
    shop: str
    supplier: str
    created_at: datetime
    expires_at: datetime
    options: ShopImportOptions
    prices_with_vat: bool
    items: list[ImportItem]
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    create_validation_field: bool = False
    shop_check: ShopCheck | None = None
    price_lines: list[ImportPriceLine] = Field(default_factory=list)
    sale_price_overrides: dict[int, Decimal] = Field(default_factory=dict)


class ShopImportResult(BaseModel):
    preview_id: str
    shop: str
    options: ShopImportOptions
    prices_with_vat: bool
    status: Literal["queued", "running", "completed", "failed"]
    items: list[ImportItem]
    errors: list[str] = Field(default_factory=list)
    updated_at: datetime
    shop_check: ShopCheck | None = None
