"""Cyklo Zitny typed XML exports; use explicit EUR prices and supplier codes."""
from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
from pathlib import Path

from lxml import etree

from inventory_hub.adapters.paul_lange_catalog import _number, _source_fields, http_url
from inventory_hub.adapters.pl_feed_convert import _get_text
from inventory_hub.catalog_types import CatalogPrices, CatalogProduct
from inventory_hub.supplier_prefix import canonical_supplier_sku, get_supplier_prefix


def read_export(path, *, include_categories=True):
    tree = etree.parse(str(path), etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
    root = tree.getroot()
    if tree.docinfo.doctype or root.tag != "export":
        raise ValueError("Expected Cyklo Zitny XML export without a DTD")
    groups = root.findall("items[@type='products']")
    dictionaries = root.findall("items[@type='categories']")
    if len(groups) != 1 or (include_categories and len(dictionaries) > 1) or any(child.tag != "item" for child in groups[0]):
        raise ValueError("Cyklo Zitny requires a complete products export, not an ID-only availability export")
    categories = {}
    for item in dictionaries[0] if include_categories and dictionaries else []:
        code = _get_text(item, "id")
        if not code or code in categories:
            raise ValueError("Invalid or duplicate Cyklo Zitny category identity")
        categories[code] = (_get_text(item, "parent_id"), _get_text(item, "name"))
    if not len(groups[0]) or len(groups[0]) > 100000:
        raise ValueError("Empty or oversized Cyklo Zitny product export")
    return list(groups[0]), categories


def product_category(item, categories):
    candidates = []
    for node in item.findall("categories/id"):
        code = (node.text or "").strip()
        current, names, seen = code, [], set()
        while current not in ("", "0") and current in categories and current not in seen:
            seen.add(current)
            current, name = categories[current]
            names.insert(0, name)
        if names and current in ("", "0") and all(names):
            candidates.append((code, names))
    # Use a real supplied hierarchy; ties keep supplier order. Never correlate
    # parallel categorytext/ID arrays (some legacy IDs have no corresponding text).
    if candidates:
        code, names = max(candidates, key=lambda candidate: len(candidate[1]))
        return code, " / ".join(names)
    return None, None


def inspection_categories(path):
    items, categories = read_export(path)
    counts, labels = Counter(), {}
    for item in items:
        code, label = product_category(item, categories)
        if code:
            counts[code] += 1
            labels[code] = label
    options = [{"source": code, "code": code, "label": labels[code], "path": labels[code], "count": count}
               for code, count in counts.items()]
    return {"source_categories": sorted(labels.values()), "source_category_options": sorted(options, key=lambda row: row["label"]),
            "category_fields": {}}


def read_stock(path):
    items, _categories = read_export(path, include_categories=False)
    for item in items:
        code, stock = _get_text(item, "code"), _get_text(item, "store_count") or None
        error = ("supplier_availability_invalid_identity" if len(item.findall("code")) != 1 else
                 "supplier_availability_invalid_quantity" if len(item.findall("store_count")) > 1 else None)
        yield code, stock, error


def parse_catalog(path: Path, supplier: str, cfg: dict, feed_key="products"):
    items, categories = read_export(path)
    adapter = cfg.get("adapter_settings") or {}
    configured_vat = adapter.get("vat_rate", adapter.get("vat", cfg.get("vat_rate")))
    try:
        vat = Decimal(str(configured_vat)) if configured_vat is not None else None
    except (ValueError, InvalidOperation):
        raise ValueError("Invalid configured Cyklo Zitny destination VAT") from None
    if vat is not None and (not vat.is_finite() or not 0 <= vat <= 100):
        raise ValueError("Invalid configured Cyklo Zitny destination VAT")
    policy = (adapter.get("catalog") or {}).get("retail_vat_policy", "preserve_gross")
    if policy not in ("preserve_gross", "preserve_net"):
        raise ValueError("Invalid Cyklo Zitny retail VAT policy")
    factor = 1 + vat / 100 if vat is not None else None
    money = lambda value: value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    prefix, output, seen = get_supplier_prefix(cfg), [], set()
    for item in items:
        code, name = _get_text(item, "code"), _get_text(item, "name")
        if (len(item.findall("code")) != 1 or len(item.findall("name")) != 1
                or not code or len(code) > 100 or not name or len(name) > 500 or code in seen):
            raise ValueError("Invalid or duplicate Cyklo Zitny product identity")
        seen.add(code)
        warnings = [] if vat is not None else ["missing_vat"]
        retail_tag = "price_moc_eur" if policy == "preserve_gross" else "price_moc_novat_eur"
        if any(len(item.findall(tag)) > 1 for tag in ("price_buy_novat_eur", retail_tag, "store_count")):
            raise ValueError("Ambiguous Cyklo Zitny price or stock field")
        purchase = _number(item, "price_buy_novat_eur", warnings, price=True)
        retail = _number(item, retail_tag, warnings, price=True)
        if policy == "preserve_net":
            retail = money(retail * factor) if retail is not None and factor is not None else None
        if retail is None or retail <= 0:
            warnings.append("missing_retail_price")
        if retail is not None and purchase is not None and factor is not None and retail < purchase * factor:
            warnings.append("retail_below_purchase")
        eans = list(dict.fromkeys((node.text or "").strip() for node in item.findall("ean") if (node.text or "").strip()))
        images = list(dict.fromkeys(url for node in item.findall("images/url") if (url := http_url(node.text))))
        if not eans:
            warnings.append("missing_ean")
        if not images:
            warnings.append("missing_images")
        category_code, category = product_category(item, categories)
        if not category:
            warnings.append("missing_category")
        raw_stock = _get_text(item, "store_count") or None
        from inventory_hub.services.supplier_availability_source import quantity, AvailabilityError
        try:
            available, stock, kind = quantity(raw_stock)
        except AvailabilityError:
            available, stock, kind = None, None, "unknown"
            warnings.append("invalid_number:store_count")
        xml = etree.tostring(item, encoding="unicode", with_tail=False)
        product = CatalogProduct(supplier=supplier, feed_key=feed_key, code=code, name=name,
            shop_code=canonical_supplier_sku(prefix, code), brand=_get_text(item, "manufacturer") or None,
            eans=eans, images=images, description=_get_text(item, "description_full"),
            short_description=_get_text(item, "description"), category_code=category_code, category=category,
            prices=CatalogPrices(currency="EUR", vat_percent=vat, vat_source="configured", purchase_net=purchase,
                purchase_gross=money(purchase * factor) if purchase is not None and factor is not None else None,
                retail_gross=retail, retail_net=money(retail / factor) if retail is not None and factor is not None else None),
            supplier_stock=stock if kind == "exact" else None, supplier_stock_min=stock if kind == "minimum" else None,
            supplier_stock_raw=raw_stock, supplier_available=available,
            delivery_date=_get_text(item, "delivery_date") or None,
            warnings=warnings, source_hash=hashlib.sha256((xml + "\n" + (category or "")).encode()).hexdigest())
        # The export provides no explicit family/variant relationship. Similar
        # names, repeated category IDs or the internal item ID cannot create one.
        output.append((product, {"xml": xml, "fields": _source_fields(item)}))
    return output
