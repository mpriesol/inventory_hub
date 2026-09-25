"""Paul Lange listing feed. Supplier facts stay separate from shop pricing."""
from __future__ import annotations

import hashlib
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from lxml import etree

from inventory_hub.adapters.pl_feed_convert import _get_text, read_feed_items
from inventory_hub.catalog_types import CatalogParameter, CatalogPrices, CatalogProduct
from inventory_hub.supplier_prefix import canonical_supplier_sku, get_supplier_prefix


def http_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parts = urlsplit(value.strip())
        if parts.scheme in ("http", "https") and parts.hostname and not parts.username and not parts.password:
            return value.strip()
    except ValueError:
        pass
    return None


def _number(item, tag: str, warnings: list[str], *, price: bool = False) -> Decimal | None:
    value = _get_text(item, tag)
    if not value:
        return None
    try:
        number = Decimal(value.replace("\xa0", "").replace(" ", "").replace(",", "."))
        if not number.is_finite() or (price and number < 0):
            raise InvalidOperation
        return number
    except InvalidOperation:
        warnings.append(f"invalid_number:{tag}")
        return None


def _source_fields(item) -> Any:
    if not len(item) and not item.attrib:
        return item.text or ""
    result: dict[str, Any] = {}
    if item.attrib:
        result["@attributes"] = dict(item.attrib)
    if item.text and item.text.strip():
        result["#text"] = item.text
    for child in item:
        if not isinstance(child.tag, str):
            continue
        result.setdefault(child.tag, []).append(_source_fields(child))
    return result


def parse_catalog(path: Path, supplier: str, cfg: dict, feed_key: str = "products") -> list[tuple[CatalogProduct, dict]]:
    adapter = cfg.get("adapter_settings") or {}
    prefix = get_supplier_prefix(cfg)
    catalog = adapter.get("catalog") or {}
    currency = str(catalog.get("currency") or cfg.get("default_currency") or "EUR").upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("Invalid catalog currency")
    vat = Decimal(str(adapter.get("vat", 23)))
    if not vat.is_finite() or not 0 <= vat <= 100:
        raise ValueError("Invalid supplier VAT setting")
    # A group is accepted only from an explicit identifier, never PRODUCT_NAME.
    group_tag = catalog.get("group_tag") or "ITEMGROUP_ID"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:/[A-Za-z_][A-Za-z0-9_]*)*", group_tag):
        raise ValueError("Invalid catalog group_tag")
    variant_names = catalog.get("variant_parameters") or []
    products = []
    seen: set[str] = set()
    for item in read_feed_items(path):
        code = _get_text(item, "ITEM_ID")
        name = _get_text(item, "PRODUCT")
        if not code or not name:
            raise ValueError("Feed contains an item without ITEM_ID or PRODUCT")
        if code in seen:
            raise ValueError(f"Duplicate supplier code: {code}")
        if len(code) > 100 or len(name) > 500:
            raise ValueError("Supplier code or name exceeds catalog field length")
        seen.add(code)
        warnings: list[str] = []
        params = [CatalogParameter(name=_get_text(p, "DESC"), value=_get_text(p, "VAL"))
                  for p in item.findall("DYN_PARAMS/PARAM") if _get_text(p, "DESC")]
        eans = list(dict.fromkeys(v.strip() for v in re.split(r"[;,|/\s]+", _get_text(item, "EAN")) if v.strip()))
        image_values = [_get_text(item, "IMGURL")] + [e.text or "" for e in item.findall("IMAGES/IMGURL")]
        images = list(dict.fromkeys(url for value in image_values if (url := http_url(value))))
        if not images:
            warnings.append("missing_images")
        if not eans:
            warnings.append("missing_ean")
        group = _get_text(item, group_tag) or None
        if group and len(group) > 100:
            raise ValueError("Supplier group code exceeds catalog field length")
        price_fields = {name: _number(item, tag, warnings, price=True) for name, tag in (
            ("purchase_net", "PRICE_VOC"), ("purchase_gross", "PRICE_VOC_VAT"),
            ("retail_net", "PRICE"), ("retail_gross", "PRICE_VAT"),
            ("discount_net", "PRICE_DISCOUNT"), ("discount_gross", "PRICE_DISCOUNT_VAT"),
        )}
        if price_fields["retail_gross"] is None and price_fields["retail_net"] is None:
            warnings.append("missing_retail_price")
        if any(price_fields[basis + "_gross"] is not None and price_fields[basis + "_net"] is not None
               and abs(price_fields[basis + "_gross"] - price_fields[basis + "_net"] * (1 + vat / 100)) > Decimal("0.02")
               for basis in ("retail", "purchase")):
            warnings.append("invalid_price_vat")
        source_xml = etree.tostring(item, encoding="unicode", with_tail=False)
        stock_raw = _get_text(item, "STOCK")
        external_raw = _get_text(item, "STOCK_EXTERNAL")
        stock_min = Decimal(stock_raw[:-1]) if re.fullmatch(r"\d+\+", stock_raw) else None
        external_flag = {"ano": True, "áno": True, "nie": False}.get(external_raw.casefold())
        static = item.find("STA_PARAMS")
        product = CatalogProduct(
            supplier=supplier, feed_key=feed_key, code=code, shop_code=canonical_supplier_sku(prefix, code),
            manufacturer_code=_get_text(item, "MANUFACTURER_CODE") or None,
            eans=eans, name=name, brand=_get_text(item, "MANUFACTURER") or None,
            description=_get_text(item, "DESCRIPTION"),
            manufacturer_description=_get_text(item, "DESC_MANUFACTURER"),
            safety_information=_get_text(item, "DESC_WARNING"),
            category=_get_text(item, "CATEGORYTEXT") or None,
            category_code=_get_text(item, "CATEGORYID") or None,
            url=http_url(_get_text(item, "URL")), images=images, parameters=params,
            static_parameters=_source_fields(static) if static is not None and len(static) else {},
            prices=CatalogPrices(currency=currency, vat_percent=vat, **price_fields),
            availability=_get_text(item, "DELIVERY") or None,
            supplier_stock=_number(item, "STOCK", warnings) if stock_min is None else None,
            supplier_stock_min=stock_min, supplier_stock_raw=stock_raw or None,
            supplier_stock_external=_number(item, "STOCK_EXTERNAL", warnings) if external_flag is None else None,
            supplier_stock_external_raw=external_raw or None, supplier_external_available=external_flag,
            delivery_date=_get_text(item, "DELIVERY_DATE") or None,
            group_code=group,
            group_name=(_get_text(item, "STA_PARAMS/PRODUCT_NAME") or name) if group else None,
            variant_attributes=[p for p in params if not variant_names or p.name in variant_names] if group else [],
            variant_relationship="explicit" if group else "not_provided",
            warnings=warnings, source_hash=hashlib.sha256(source_xml.encode()).hexdigest(),
        )
        products.append((product, {"xml": source_xml, "fields": _source_fields(item)}))
    if not products:
        raise ValueError("The listing feed is empty")
    return products
