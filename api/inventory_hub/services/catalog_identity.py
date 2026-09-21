"""Read the connection-scoped shop index without making Upgates requests."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from inventory_hub import config_io
from inventory_hub.catalog_types import CatalogProduct, CatalogShopMatch


def connection_fingerprint(cfg: dict) -> str:
    return hashlib.sha256(json.dumps([cfg.get(k) for k in
        ("upgates_api_base_url", "upgates_login", "upgates_api_key")]).encode()).hexdigest()


def read_cache(path: Path, fingerprint: str, at: datetime) -> dict | None:
    try:
        value = json.loads(path.read_text())
        if value.get("version") == 1 and value.get("fingerprint") == fingerprint:
            checked = datetime.fromisoformat(value["checked_at"])
            if checked.tzinfo and checked <= at:
                return value
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def parent_shop_code(product: CatalogProduct) -> str:
    if not product.group_code or product.variant_relationship != "explicit":
        return product.shop_code
    prefix = product.shop_code[:-len(product.code)] if product.shop_code.endswith(product.code) else ""
    return prefix + "G-" + product.group_code


class ShopIdentityIndex:
    def __init__(self, saved: dict | None = None):
        self.checked_at = saved.get("checked_at") if saved else None
        self.codes: dict[str, list[dict]] = {}
        self.eans: dict[str, list[dict]] = {}
        for parent_id, entries in (saved or {}).get("products", {}).items():
            parent_code = entries[0].get("display_code", entries[0]["code"]) if entries else ""
            for entry in entries:
                target = {"code": entry.get("display_code", entry["code"]), "parent_code": parent_code,
                          "remote_product_id": None if parent_id.startswith("code:") else parent_id}
                if entry["code"]:
                    self.codes.setdefault(entry["code"].casefold(), []).append(target)
                for ean in entry["eans"]:
                    if ean:
                        self.eans.setdefault(ean, []).append(target)

    def matches(self, product: CatalogProduct, *, include_parent: bool = True) -> list[CatalogShopMatch]:
        output = []
        for by, value, entries in [("code", product.shop_code, self.codes.get(product.shop_code.casefold(), [])),
                                   *(("ean", ean, self.eans.get(ean, [])) for ean in product.eans)]:
            output.extend(CatalogShopMatch(matched_by=by, value=value, **entry) for entry in entries)
        parent_code = parent_shop_code(product)
        if include_parent and not output and parent_code != product.shop_code:
            output.extend(CatalogShopMatch(matched_by="parent_code", value=parent_code, **entry)
                          for entry in self.codes.get(parent_code.casefold(), []))
        return output


def cached_identities(shop: str | None) -> ShopIdentityIndex:
    if not shop or not re.fullmatch(r"[a-zA-Z0-9_-]{1,50}", shop) or not config_io.shop_path(shop).is_file():
        return ShopIdentityIndex()
    cfg = config_io.load_shop(shop)
    saved = read_cache(config_io.shop_path(shop).parent / "catalog-cache" / "identities.json",
                       connection_fingerprint(cfg), datetime.now(timezone.utc))
    try:
        if saved and isinstance(saved.get("products"), dict):
            full_at = datetime.fromisoformat(saved["full_checked_at"])
            if full_at.tzinfo and full_at <= datetime.fromisoformat(saved["checked_at"]):
                return ShopIdentityIndex(saved)
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    return ShopIdentityIndex()
