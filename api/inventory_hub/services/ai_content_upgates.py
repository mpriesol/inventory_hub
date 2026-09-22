"""Content metadata and readback checks, separate from stock synchronization."""
from datetime import datetime, timedelta

from inventory_hub.services import catalog_import as imports
from inventory_hub.services.catalog import CatalogError
from inventory_hub.services.upgates import UpgatesError

FIELDS = [
    {"key": "h1_descriptor", "label": "H1 – typ produktu", "common_languages_value_yn": False},
    {"key": "future_name", "label": "H1 – značka a model", "common_languages_value_yn": True},
    {"key": "h1_descr_suffix", "label": "H1 – doplnok", "common_languages_value_yn": False},
    {"key": "supplier_name", "label": "Dodávateľ", "common_languages_value_yn": False},
]


def parameter_registry(shop, client):
    """Read names for category-rule mapping; reuse the result for one day."""
    with imports._shop_cache(shop, "ai-parameter-registry") as (path, saved, fingerprint):
        if saved and imports.now() - datetime.fromisoformat(saved["checked_at"]) < timedelta(days=1):
            return {"checked_at": saved["checked_at"], "parameters": saved["data"]}
        rows = imports._pages(client, "parameters", "parameters", {"without_values_yn": "TRUE"})
        data = [{"id": row["id"], "names": {d["language"]: d.get("name", "") for d in row.get("descriptions", [])}}
                for row in rows]
        checked_at = imports.now().isoformat()
        imports._write(path, {"version": 1, "fingerprint": fingerprint, "checked_at": checked_at, "data": data})
        return {"checked_at": checked_at, "parameters": data}


def content_fields(shop, client, *, create=False):
    with imports._shop_cache(shop, "ai-content-fields") as (path, saved, fingerprint):
        fresh = bool(saved and imports.now() - datetime.fromisoformat(saved["checked_at"]) < timedelta(minutes=15))
        rows = saved["data"] if fresh else imports._pages(client, "metas", "metas", {"category": "products"})
        missing = []
        for expected in FIELDS:
            actual = next((m for m in rows if m.get("key") == expected["key"] and m.get("category") == "products"), None)
            # Existing stores can use textarea for h1_descriptor. Both text types
            # are compatible; do not alter the shop's existing field definitions.
            if actual and actual.get("type") not in ("input", "textarea"):
                raise CatalogError("ai_meta_incompatible", f"Check the shop metadata definition for {expected['key']}", 422)
            if actual is None:
                missing.append({**expected, "category": "products", "type": "input", "active": True})
        if missing and create:
            try:
                client.post("metas", {"metas": missing})
            except UpgatesError:
                raise CatalogError("ai_meta_create_failed", "Content fields could not be confirmed; no product was sent", 502) from None
            rows = imports._pages(client, "metas", "metas", {"category": "products"})
            if any(not any(m.get("key") == f["key"] and m.get("category") == "products" and m.get("type") == "input"
                           and m.get("common_languages_value_yn") == f["common_languages_value_yn"] for m in rows) for f in missing):
                raise CatalogError("ai_meta_create_failed", "New content fields were not confirmed", 502)
        imports._write(path, {"version": 1, "fingerprint": fingerprint, "checked_at": imports.now().isoformat(), "data": rows})
        return {f["key"]: next((bool(m.get("common_languages_value_yn")) for m in rows
                if m.get("key") == f["key"] and m.get("category") == "products"), f["common_languages_value_yn"])
                for f in FIELDS}


def verify_content(remote: dict, expected: dict):
    from inventory_hub.services.ai_content_validation import text_of
    for description in expected.get("descriptions", []):
        if "short_description" not in description:
            continue
        actual = next((d for d in remote.get("descriptions", []) if d.get("language") == description["language"]), {})
        for key in ("title", "short_description", "long_description", "seo_title", "seo_description"):
            if text_of(actual.get(key, "")) != text_of(description.get(key, "")):
                raise CatalogError("ai_content_readback_mismatch", f"The shop did not confirm the imported {key}", 409)
    for meta in expected.get("metas", []):
        if meta["key"] not in {f["key"] for f in FIELDS}:
            continue
        values = meta.get("values") or []
        languages = (list(values) if isinstance(values, dict) else [v["language"] for v in values])
        if "value" in meta:
            languages = [d["language"] for d in expected.get("descriptions", [])]
        for language in languages:
            if not any(m.get("key") == meta["key"] and meta_value(m, language) == meta_value(meta, language) for m in remote.get("metas", [])):
                raise CatalogError("ai_content_readback_mismatch", "The shop did not confirm the imported content fields", 409)


def meta_value(meta: dict, language: str):
    if "value" in meta:
        return str(meta["value"])
    values = meta.get("values") or {}
    value = values.get(language) if isinstance(values, dict) else next((v for v in values if v.get("language") == language), None)
    if isinstance(value, dict):
        value = value.get("value")
    return None if value is None else str(value)
