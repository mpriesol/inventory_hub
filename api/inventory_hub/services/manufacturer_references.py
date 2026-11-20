"""Documented part numbers for individual offers without a confirmed feed field."""
import re

from pydantic import BaseModel, ConfigDict, HttpUrl, StrictStr, ValidationError, field_validator

from inventory_hub import config_io
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.sku_policy_types import SkuIdentityInput, exact_text


class ManufacturerReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brand: StrictStr
    gtin: StrictStr
    manufacturer_code: StrictStr
    source_ref: StrictStr

    @field_validator("brand")
    @classmethod
    def brand_text(cls, value):
        return exact_text(value, 100)

    @field_validator("gtin")
    @classmethod
    def verified_gtin(cls, value):
        if not ProductIdentifierService.gtin_key(value):
            raise ValueError("A verified package barcode is required")
        return value

    @field_validator("manufacturer_code")
    @classmethod
    def part_number(cls, value):
        return SkuIdentityInput.code_text(value)

    @field_validator("source_ref")
    @classmethod
    def evidence_url(cls, value):
        url = HttpUrl(exact_text(value, 2000))
        if url.username or url.password:
            raise ValueError("Use a public product reference without credentials")
        return value


def validate_references(feeds):
    if not isinstance(feeds, dict):
        raise ValueError("Manufacturer references must be grouped by listing feed")
    for feed_key, offers in feeds.items():
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,50}", feed_key) or not isinstance(offers, dict):
            raise ValueError("Each listing feed needs a map of supplier codes")
        for supplier_code, reference in offers.items():
            exact_text(supplier_code, 100)
            ManufacturerReference.model_validate(reference)


def supplier_references(supplier):
    if not config_io.supplier_path(supplier).is_file():
        return {}
    cfg = config_io.load_supplier(supplier, write_back_on_load=False)
    return (cfg.get("adapter_settings") or {}).get("manufacturer_references") or {}


def verified_manufacturer_item(source, references):
    """A supplier code locates the proof. Brand and the unique pack GTIN must also match."""
    if not isinstance(references, dict):
        return None
    offers = references.get(source.get("feed_key") or "products")
    if not isinstance(offers, dict) or source.get("code") not in offers:
        return None
    try:
        item = ManufacturerReference.model_validate(offers[source["code"]])
    except ValidationError:
        return None
    source_brand = " ".join((source.get("brand") or "").casefold().split())
    if source_brand != " ".join(item.brand.casefold().split()):
        return None
    barcode_keys = {ProductIdentifierService.gtin_key(value) for value in source.get("eans") or []}
    if barcode_keys != {ProductIdentifierService.gtin_key(item.gtin)}:
        return None
    return {"brand": source["brand"], "source_code": item.manufacturer_code, "code_kind": "manufacturer",
            "leaf_confirmed": True, "source_ref": item.source_ref}
