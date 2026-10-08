"""Read-only mapping suggestions from shop names and published category registers."""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from inventory_hub.ai_content_models import AiRuleState, AiRuleVersion
from inventory_hub.ai_content_types import RuleBook
from inventory_hub.db_models import Shop
from inventory_hub.services import ai_content_upgates, catalog, catalog_import, feed_mapping
from inventory_hub.services.upgates import UpgatesClient, UpgatesError


def category_profiles(book: RuleBook, shop: str):
    """Category selection filters suggestions; it does not narrow saved bindings."""
    profiles = []
    for profile in book.categories:
        parameters = [parameter.model_dump(include={"name", "required", "scope", "values", "unit"})
                      for parameter in profile.parameters if parameter.approved]
        if not parameters:
            continue
        codes = [profile.shop_categories.get(shop), *profile.shop_category_matches.get(shop, [])] if shop else []
        profiles.append({"id": profile.id, "name": profile.name, "registry_status": profile.registry_status,
                         "category_codes": list(dict.fromkeys(code for code in codes if code)),
                         "parameters": parameters})
    return profiles


async def parameter_options(db, supplier: str, feed_key: str = "products", shop: str = "", *, refresh=False):
    feed_mapping._listing_scope(catalog.supplier_config(supplier), feed_key)
    if shop and not await db.scalar(select(Shop.id).where(Shop.code == shop, Shop.is_active.is_(True))):
        raise catalog.CatalogError("shop_not_found", "Choose an active target shop", 404)
    # Do not seed or publish a rule book while reading mapping suggestions.
    state = await db.get(AiRuleState, 1)
    version = await db.get(AiRuleVersion, state.published_id) if state else None
    book = RuleBook.model_validate(version.book) if version else RuleBook()
    result = {"rules_version": version.id if version else None, "checked_at": None,
              "parameters": [], "category_profiles": category_profiles(book, shop), "warnings": []}
    if not shop:
        return result
    try:
        catalog_import.shop_config(shop)
        registry = await asyncio.to_thread(ai_content_upgates.parameter_registry, shop,
                                           UpgatesClient.from_shop(shop), refresh=refresh)
        # Return only the documented names, not remote metadata or credentials.
        parameters = []
        for row in registry["parameters"][:10000]:
            names = {language: name for language, name in row["names"].items()
                     if isinstance(language, str) and len(language) <= 20 and isinstance(name, str)
                     and 0 < len(name.strip()) <= 100 and not any(ord(c) < 32 for c in name)}
            if names and isinstance(row["id"], (int, str)) and len(str(row["id"])) <= 100:
                parameters.append({"id": row["id"], "names": names})
        result.update({"checked_at": registry["checked_at"], "parameters": parameters})
        if len(registry["parameters"]) > 10000:
            result["warnings"].append("upgates_parameter_registry_truncated")
    except (catalog.CatalogError, UpgatesError, OSError, ValueError, KeyError, TypeError):
        # The rule register remains useful during shop outages. Never expose
        # upstream responses, which may contain connection configuration.
        result["warnings"].append("upgates_parameter_registry_unavailable")
    return result
