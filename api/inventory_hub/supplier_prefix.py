"""Supplier identifiers are text; aliases must never select different SKU prefixes."""
from __future__ import annotations

from copy import deepcopy
import re


class SupplierPrefixError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.status = code, status


PREFIX_PATHS = (
    ("adapter_settings", "mapping", "postprocess", "product_code_prefix"),
    ("adapter_settings", "product_code_prefix"),
    ("product_code_prefix",),
)
READ_ONLY_FIELDS = ("product_prefix", "product_prefix_locked", "product_prefix_lock_reason")


def prefix_values(cfg: dict, *, strict: bool = True) -> list[str]:
    values = []
    for path in PREFIX_PATHS:
        value = cfg
        for part in path:
            if not isinstance(value, dict) or part not in value:
                break
            value = value[part]
        else:
            if value is None:
                value = ""
            if not isinstance(value, str) or (value and not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", value)):
                if strict:
                    raise SupplierPrefixError("supplier_prefix_invalid", "Prefix must be text with at most 20 letters, digits, hyphens or underscores")
                continue
            values.append(value)
    return values


def get_supplier_prefix(cfg: dict) -> str:
    values = set(value for value in prefix_values(cfg) if value)
    if len(values) > 1:
        raise SupplierPrefixError("supplier_prefix_conflict", "Supplier configuration contains conflicting product prefix aliases", 409)
    return next(iter(values), "")


def normalize_prefix_config(cfg: dict) -> dict:
    """Canonicalize old alias locations without changing the prefix's spelling."""
    result = deepcopy(cfg)
    section = result
    for key in ("adapter_settings", "mapping", "postprocess"):
        if key not in section:
            break
        section = section[key]
        if not isinstance(section, dict):
            raise SupplierPrefixError("supplier_prefix_invalid", "Supplier adapter settings and prefix mapping must be objects")
    values = prefix_values(result)
    prefix = get_supplier_prefix(result)
    for field in READ_ONLY_FIELDS:
        result.pop(field, None)
    result.pop("product_code_prefix", None)
    adapter = result.get("adapter_settings")
    if isinstance(adapter, dict):
        adapter.pop("product_code_prefix", None)
    if values:
        adapter = result.setdefault("adapter_settings", {})
        if not isinstance(adapter, dict):
            raise SupplierPrefixError("supplier_prefix_invalid", "Supplier adapter settings must be an object")
        mapping = adapter.setdefault("mapping", {})
        if not isinstance(mapping, dict):
            raise SupplierPrefixError("supplier_prefix_invalid", "Supplier prefix mapping must be an object")
        postprocess = mapping.setdefault("postprocess", {})
        if not isinstance(postprocess, dict):
            raise SupplierPrefixError("supplier_prefix_invalid", "Supplier prefix mapping must be an object")
        postprocess["product_code_prefix"] = prefix
    return result


def canonical_supplier_sku(prefix: str, supplier_code: str) -> str:
    """Concatenate an explicit prefix and a raw variant code; never strip zeros."""
    if not isinstance(supplier_code, str) or not supplier_code or supplier_code != supplier_code.strip():
        raise SupplierPrefixError("supplier_code_invalid", "Supplier variant code must be non-empty text without surrounding whitespace")
    if any(ord(char) < 32 or ord(char) == 127 for char in supplier_code):
        raise SupplierPrefixError("supplier_code_invalid", "Supplier variant code contains a control character")
    get_supplier_prefix({"product_code_prefix": prefix})
    if len(prefix) + len(supplier_code) > 100:
        raise SupplierPrefixError("supplier_code_invalid", "Prefix and supplier variant code together must not exceed 100 characters")
    return prefix + supplier_code
