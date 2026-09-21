"""Content metadata and readback checks, separate from stock synchronization."""
from datetime import datetime, timedelta

from inventory_hub.services import catalog_import as imports
from inventory_hub.services.catalog import CatalogError
from inventory_hub.services.upgates import UpgatesError

FIELDS = [
    {"key": "h1_descriptor", "label": "H1 – typ produktu", "common_languages_value_yn": False},
    {"key": "future_name", "label": "H1 – značka a model", "common_languages_value_yn": True},
    {"key": "h1_descr_suffix", "label": "H1 – doplnok", "common_languages_value_yn": False},
]


def content_fields(shop, client, *, create=False):
    with imports._shop_cache(shop, "ai-content-fields") as (path, saved, fingerprint):
        fresh = bool(saved and imports.now() - datetime.fromisoformat(saved["checked_at"]) < timedelta(minutes=15))
        rows = saved["data"] if fresh else imports._pages(client, "metas", "metas", {"category": "products"})
        missing = []
        for expected in FIELDS:
            actual = next((m for m in rows if m.get("key") == expected["key"] and m.get("category") == "products"), None)
            if actual and (actual.get("type") != "input" or actual.get("common_languages_value_yn") != expected["common_languages_value_yn"]):
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
        return [m["key"] for m in missing]


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
        if not any(m.get("key") == meta["key"] and m.get("value") == meta["value"] and
                   ("language" not in meta or m.get("language") == meta["language"]) for m in remote.get("metas", [])):
            raise CatalogError("ai_content_readback_mismatch", "The shop did not confirm the imported H1 content fields", 409)
