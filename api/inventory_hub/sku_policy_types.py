"""Explicit, lossless evidence for a concrete sale unit, never a parent code."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator


class PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def exact_text(value: str, maximum: int) -> str:
    if not value or value != value.strip() or len(value) > maximum:
        raise ValueError("Supply the original non-empty value without surrounding whitespace")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Control characters are not allowed")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("Invalid text") from None
    return value


class SkuIdentityInput(PolicyModel):
    brand: StrictStr
    source_code: StrictStr
    code_kind: Literal["manufacturer", "supplier", "gtin", "service", "unbranded"]
    source_ref: StrictStr
    leaf_confirmed: StrictBool
    supplier_id: StrictInt | None = Field(default=None, gt=0)

    @field_validator("brand")
    @classmethod
    def brand_text(cls, value):
        return exact_text(value, 100)

    @field_validator("source_code")
    @classmethod
    def code_text(cls, value):
        exact_text(value, 97)
        if ";" in value:
            raise ValueError("Semicolons are not supported in product codes")
        return value

    @field_validator("source_ref")
    @classmethod
    def evidence_text(cls, value):
        return exact_text(value, 2000)


class BrandCreate(PolicyModel):
    name: StrictStr
    code: StrictStr = Field(pattern=r"^[A-Z]{2}$")
    kind: Literal["brand", "service", "unbranded"] = "brand"
    aliases: list[StrictStr] = Field(default_factory=list, max_length=50)
    source_ref: StrictStr

    @field_validator("name")
    @classmethod
    def name_text(cls, value):
        return exact_text(value, 100)

    @field_validator("aliases")
    @classmethod
    def alias_text(cls, values):
        return [exact_text(value, 100) for value in values]

    @field_validator("source_ref")
    @classmethod
    def evidence_text(cls, value):
        return exact_text(value, 2000)


class BrandAliasCreate(PolicyModel):
    alias: StrictStr
    source_ref: StrictStr

    @field_validator("alias")
    @classmethod
    def alias_text(cls, value):
        return exact_text(value, 100)

    @field_validator("source_ref")
    @classmethod
    def evidence_text(cls, value):
        return exact_text(value, 2000)


class SkuPreviewRequest(PolicyModel):
    sku_identity: SkuIdentityInput | None = None
    sku: StrictStr = Field(default="", max_length=100)
    barcodes: list[StrictStr] = Field(default_factory=list, max_length=20)
    supplier_id: StrictInt | None = Field(default=None, gt=0)
    supplier_sku: StrictStr = Field(default="", max_length=100)
    expected_product_id: StrictInt | None = Field(default=None, gt=0)
