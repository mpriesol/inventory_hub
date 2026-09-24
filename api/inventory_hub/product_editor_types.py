"""Whitelisted editor fields; validation is per row to retain partial batch success."""
from decimal import Decimal
import re
from typing import Literal
from uuid import UUID
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator


class EditorModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def safe_text(value, *, maximum, blank=True):
    if value is None:
        return value
    if not isinstance(value, str) or len(value) > maximum or (not blank and not value.strip()):
        raise ValueError("Invalid text")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("Invalid text") from None
    if any(ord(char) < 32 and char not in "\n\t" for char in value) or "\x7f" in value:
        raise ValueError("Invalid text")
    return value


def decimal_text(value, *, maximum, places):
    if value is None:
        return value
    pattern = r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1," + str(places) + r"})?" if places else r"(?:0|[1-9][0-9]*)"
    if not isinstance(value, str) or len(value) > 20 or re.fullmatch(pattern, value) is None:
        raise ValueError("A decimal string is required")
    number = Decimal(value)
    if number > maximum:
        raise ValueError("Value is too large")
    return format(number, f".{places}f") if places else str(int(number))


class CommonPatch(EditorModel):
    name: StrictStr | None = None
    brand: StrictStr | None = None
    internal_note: StrictStr | None = None
    image_url: StrictStr | None = None

    @field_validator("image_url")
    @classmethod
    def image_valid(cls, value):
        if value is None:
            return None
        safe_text(value, maximum=2000, blank=False)
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("HTTPS image URL required")
        return value

    @field_validator("name")
    @classmethod
    def name_valid(cls, value):
        return safe_text(value, maximum=500, blank=False)

    @field_validator("brand")
    @classmethod
    def brand_valid(cls, value):
        return safe_text(value, maximum=100)

    @field_validator("internal_note")
    @classmethod
    def note_valid(cls, value):
        return safe_text(value, maximum=4000)


class AttributePatch(EditorModel):
    name: StrictStr
    value: StrictStr

    @field_validator("name")
    @classmethod
    def name_valid(cls, value):
        return safe_text(value, maximum=100, blank=False).strip()

    @field_validator("value")
    @classmethod
    def value_valid(cls, value):
        return safe_text(value, maximum=255, blank=False).strip()


class VariantPatch(EditorModel):
    sale_price_gross: StrictStr | None = None
    vat_rate: StrictStr | None = None
    note: StrictStr | None = None
    attributes: list[AttributePatch] | None = Field(default=None, max_length=100)
    eans: list[StrictStr] | None = Field(default=None, max_length=20)
    sku: StrictStr | None = None

    @field_validator("sku")
    @classmethod
    def sku_valid(cls, value):
        if value is None:
            return None
        safe_text(value, maximum=100, blank=False)
        if value != value.strip() or ";" in value:
            raise ValueError("Invalid code")
        return value

    @field_validator("eans")
    @classmethod
    def eans_valid(cls, value):
        if value is None:
            return None
        from inventory_hub.services.identifiers import ProductIdentifierService
        if len(value) != len(set(value)) or any(not code.isascii() or not code.isdigit() or not code.strip("0")
                or ProductIdentifierService.classify_barcode(code) not in ProductIdentifierService.BARCODE_TYPES
                for code in value):
            raise ValueError("Invalid barcode")
        return value

    @field_validator("attributes")
    @classmethod
    def attributes_valid(cls, value):
        if value is not None and len({item.name.casefold() for item in value}) != len(value):
            raise ValueError("Duplicate attribute")
        return value

    @field_validator("sale_price_gross")
    @classmethod
    def price_valid(cls, value):
        return decimal_text(value, maximum=Decimal("9999999999.99"), places=2)

    @field_validator("vat_rate")
    @classmethod
    def vat_valid(cls, value):
        return decimal_text(value, maximum=Decimal("100"), places=2)

    @field_validator("note")
    @classmethod
    def note_valid(cls, value):
        return safe_text(value, maximum=4000)


class WarehousePatch(EditorModel):
    location: StrictStr | None = None
    min_quantity: StrictStr | None = None

    @field_validator("location")
    @classmethod
    def location_valid(cls, value):
        return safe_text(value, maximum=100)

    @field_validator("min_quantity")
    @classmethod
    def quantity_valid(cls, value):
        return decimal_text(value, maximum=Decimal("999999999"), places=0)


class ShopPatch(EditorModel):
    name: StrictStr | None = None
    sale_price_gross: StrictStr | None = None
    visible: StrictBool | None = None

    @field_validator("name")
    @classmethod
    def name_valid(cls, value):
        return safe_text(value, maximum=500, blank=False)

    @field_validator("sale_price_gross")
    @classmethod
    def price_valid(cls, value):
        return decimal_text(value, maximum=Decimal("9999999999.99"), places=2)


class EditorRowPatch(EditorModel):
    product_id: StrictInt = Field(gt=0)
    expected_revision: StrictInt = Field(ge=0)
    snapshot_hash: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    common: CommonPatch | None = None
    variant: VariantPatch | None = None
    warehouse: WarehousePatch | None = None
    shops: dict[StrictStr, ShopPatch] | None = None

    @model_validator(mode="after")
    def has_changes(self):
        patches = self.model_dump(exclude_unset=True, exclude={"product_id", "expected_revision", "snapshot_hash"})
        if not any(value for value in patches.values()):
            raise ValueError("No edits supplied")
        if self.shops and (len(self.shops) > 20 or any(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,49}", key) is None for key in self.shops)):
            raise ValueError("Invalid shop")
        return self


class ProductEditorSaveRequest(EditorModel):
    request_id: UUID
    warehouse_code: StrictStr | None = Field(default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,49}$")
    confirmed: Literal[True]
    # Parse each patch independently in the service; one bad row does not erase valid work.
    changes: list[dict] = Field(min_length=1, max_length=100)

    @field_validator("confirmed", mode="before")
    @classmethod
    def confirmed_valid(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation required")
        return value


class PublicationPreviewRequest(EditorModel):
    shop_code: StrictStr = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,49}$")
    expected_revision: StrictInt = Field(ge=0)
    fields: list[Literal["name", "sale_price_gross", "visible", "ean", "attributes", "image_url"]] = Field(min_length=1, max_length=6)

    @field_validator("fields")
    @classmethod
    def fields_unique(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Repeated field")
        return value


class PublicationSendRequest(EditorModel):
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def exact_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation required")
        return value


class PublicationResolveRequest(PublicationSendRequest):
    original_request_settled: Literal[True]
    note: StrictStr = Field(min_length=10, max_length=1000)

    @field_validator("original_request_settled", mode="before")
    @classmethod
    def settled_confirmation(cls, value):
        if value is not True:
            raise ValueError("Confirm original request can no longer land")
        return value
