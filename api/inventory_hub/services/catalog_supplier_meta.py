"""Supplier attribution in the supported Upgates custom metadata field.

This is deliberately separate from Upgates' native supplier relation, which is
not a documented writable product API field.
"""
from copy import deepcopy

from inventory_hub.services import ai_content_upgates
from inventory_hub.services.catalog import CatalogError

KEY = "supplier_name"


def prepare_meta(shop, client, name: str, language: str, *, create=False) -> dict:
    """Prepare one field using the shop's existing language configuration.

    Preview is read-only. ``create=True`` may create this one missing custom
    field before publication, then confirms its definition with a fresh read.
    The supplier name must come from configuration, never generated content.
    """
    if not isinstance(name, str) or not name.strip():
        raise CatalogError("supplier_name_missing", "The supplier name is missing from configuration", 422)
    if not isinstance(language, str) or not language.strip():
        raise CatalogError("supplier_language_missing", "The target product language is missing", 422)
    common = ai_content_upgates.content_fields(shop, client, create=create, keys=(KEY,))[KEY]
    return {"key": KEY, **({"value": name.strip()} if common else
        {"values": [{"language": language, "value": name.strip()}]})}


def attach_meta(payload: dict, meta: dict) -> None:
    """Keep supplier attribution authoritative for the parent and each variant."""
    for value in [payload, *payload.get("variants", [])]:
        value["metas"] = [m for m in value.get("metas", []) if m.get("key") != KEY] + [deepcopy(meta)]


def verify_payload(remote: dict, expected: dict, language: str) -> None:
    """Confirm saved supplier names without weakening historical import recovery."""
    def verify_value(actual: dict, wanted: dict):
        meta = next((m for m in wanted.get("metas", []) if m.get("key") == KEY), None)
        if meta is None:
            return
        expected_value = ai_content_upgates.meta_value(meta, language)
        if expected_value is None or not any(
            m.get("key") == KEY and ai_content_upgates.meta_value(m, language) == expected_value
            for m in actual.get("metas", [])
        ):
            raise CatalogError("import_supplier_readback_mismatch", "The shop did not confirm the imported supplier name", 409)

    verify_value(remote, expected)
    variants = {v.get("code"): v for v in remote.get("variants", [])}
    for variant in expected.get("variants", []):
        verify_value(variants.get(variant.get("code"), {}), variant)
