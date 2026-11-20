"""Explicit, lossless evidence for a concrete sale unit, never a parent code."""
import re
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StrictBool, StrictInt, StrictStr, field_validator, model_validator


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
    source_ref: StrictStr = ""
    leaf_confirmed: StrictBool = False
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
        return exact_text(value, 2000) if value else ""


    @model_validator(mode="after")
    def verified_proof_has_source(self):
        if self.leaf_confirmed and not self.source_ref:
            raise ValueError("A verified identity needs its source reference")
        return self


# B2B landing pages sometimes route through a query (for example route=account/login).
# Allow plain routing values, and reject credentials, signed links and onward redirects.
_PRIVATE_PORTAL_KEYS = {"key", "sig", "sid", "pwd", "pass", "login", "username", "user", "email", "code",
                        "state", "ticket", "nonce", "url", "uri", "target", "next", "goto", "return",
                        "destination", "dest", "continue", "relaystate", "samlrequest", "samlresponse"}
_PRIVATE_PORTAL_TERMS = ("auth", "token", "secret", "signatur", "session", "credential", "password",
                         "passwd", "cookie", "apikey", "accesskey", "privatekey", "bearer", "redirect",
                         "returnurl", "callback", "jwt", "sessid")


def _public_portal_query(query):
    for key, value in parse_qsl(query, keep_blank_values=True, errors="strict", max_num_fields=32):
        normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
        if (not re.fullmatch(r"[A-Za-z0-9_.-]+", key) or normalized in _PRIVATE_PORTAL_KEYS
                or any(term in normalized for term in _PRIVATE_PORTAL_TERMS)):
            return False
        if "//" in value or not re.fullmatch(r"[\w./~-]*(?: [\w./~-]+)*", value):
            return False
    return True


def public_source_url(value, maximum):
    exact_text(value, maximum)
    parsed = urlsplit(value)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or any(char.isspace() or char in "<>\"'\\#" for char in value)
            or not _public_portal_query(parsed.query)):
        raise ValueError("Use a public HTTP(S) address without credentials, private parameters or redirects")
    HttpUrl(value)  # Also validate the browser-facing hostname and port.
    return value


class BrandSupplier(PolicyModel):
    name: StrictStr
    url: StrictStr = Field(default="", max_length=2048)

    @field_validator("name")
    @classmethod
    def supplier_name(cls, value):
        # Preserve the displayed spelling and normalize spacing for the registry.
        return exact_text(" ".join(value.split()), 100)

    @field_validator("url")
    @classmethod
    def b2b_link(cls, value):
        if not value:
            return value
        return public_source_url(value, 2048)


class BrandFields(PolicyModel):
    name: StrictStr
    code: StrictStr = Field(pattern=r"^[A-Z]{2}$")
    kind: Literal["brand", "service", "unbranded"] = "brand"
    aliases: list[StrictStr] = Field(default_factory=list)
    source_ref: StrictStr | None = Field(default=None, max_length=2000)
    suppliers: list[BrandSupplier] = Field(default_factory=list, max_length=200)

    @field_validator("suppliers")
    @classmethod
    def unique_suppliers(cls, values):
        if len({item.name.casefold() for item in values}) != len(values):
            raise ValueError("Each supplier name may appear only once")
        return sorted(values, key=lambda item: item.name.casefold())

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
    def source_lines(cls, value):
        if value is not None:
            for line in value.split("\n"):
                exact_text(line, 2000)
        return value


class BrandSnapshot(BrandFields):
    archived: StrictBool | None = None


class BrandCreate(BrandFields):
    aliases: list[StrictStr] = Field(default_factory=list, max_length=50)
    source_ref: StrictStr = Field(max_length=2000)


class BrandUpdate(BrandCreate):
    # Existing registries can have more spellings via the one-at-a-time alias endpoint.
    aliases: list[StrictStr] = Field(default_factory=list)
    expected: BrandSnapshot


class BrandRemove(PolicyModel):
    expected: BrandSnapshot


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
    item_type: Literal["stock", "service"] = "stock"
    sku: StrictStr = Field(default="", max_length=100)
    barcodes: list[StrictStr] = Field(default_factory=list, max_length=20)
    supplier_id: StrictInt | None = Field(default=None, gt=0)
    supplier_sku: StrictStr = Field(default="", max_length=100)
    expected_product_id: StrictInt | None = Field(default=None, gt=0)
