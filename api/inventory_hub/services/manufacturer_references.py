"""Documented part numbers for individual offers without a confirmed feed field."""
import re

from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError, field_validator

from inventory_hub import config_io
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.sku_policy_types import SkuIdentityInput, exact_text, public_source_url


# Retail box codes independently matched to the distributor's exact offer.
# Codes printed on another package of the same model are separate identities.
PAUL_LANGE_SHIMANO_REFERENCES = {
    "ESMRT86L2": ({"4550170818104", "0192790818108"},
                 "https://si.shimano.com/en/pdfs/sm/CY25_GENUINE_PARTS/CY25_GENUINE_PARTS_O.pdf"),
    "ESMRT86S2": ({"4550170818111", "0192790818115"},
                 "https://si.shimano.com/en/pdfs/sm/CY25_GENUINE_PARTS/CY25_GENUINE_PARTS_O.pdf"),
    "ISMRT86M2": ({"4524667744702"},
                 "https://www.mb-fahrrad.de/teile/bremsen/bremsscheiben/1465/shimano-xt-bremsscheibe-oe-180-mm-6-loch-sm-rt86-ice-tec"),
}


def native_manufacturer_item(source):
    if (source.get("supplier") != "paul-lange" or source.get("feed_key") != "products"
            or (source.get("brand") or "").strip().casefold() != "shimano"):
        return None
    code = source.get("code")
    reference = PAUL_LANGE_SHIMANO_REFERENCES.get(code)
    if reference is None:
        return None
    barcodes = {ProductIdentifierService.gtin_key(value) for value in source.get("eans", []) if value != code}
    expected = {ProductIdentifierService.gtin_key(value) for value in reference[0]}
    if not barcodes or not barcodes <= expected:
        return None
    return {"brand": source["brand"], "source_code": code, "code_kind": "manufacturer",
            "leaf_confirmed": True, "source_ref": reference[1]}


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
        return public_source_url(value, 2000)


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
