"""Northfinder listing XML: explicit families, net purchase and gross retail prices."""
from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from lxml import etree

from inventory_hub.adapters.paul_lange_catalog import _number, _source_fields, http_url
from inventory_hub.adapters.pl_feed_convert import _get_text
from inventory_hub.catalog_types import CatalogParameter, CatalogPrices, CatalogProduct
from inventory_hub.supplier_prefix import canonical_supplier_sku, get_supplier_prefix


def _url(value: str) -> str | None:
    value = value.strip()
    if value.startswith("b2b.northfinder.com/"):
        value = "https://" + value
    return http_url(value)


def parse_catalog(path: Path, supplier: str, cfg: dict, feed_key: str = "products") -> list[tuple[CatalogProduct, dict]]:
    adapter = cfg.get("adapter_settings") or {}
    mapping = adapter.get("northfinder_feed") or {}
    tags = {key: mapping.get(key) or default for key, default in (
        ("item_tag", "product"), ("variant_tag", "variant"), ("sku_field", "reference"),
        ("ean_field", "ean13"), ("qty_field", "quantity"), ("b2b_price_field", "price"),
        ("rrp_field", "recomended_retail_price"), ("rrp_sale_field", "recomended_retail_sale_price"))}
    if any(not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", tag) for tag in tags.values()):
        raise ValueError("Invalid Northfinder field mapping")
    prefix = get_supplier_prefix(cfg)
    vat = Decimal(str(adapter.get("vat", cfg.get("vat_rate", 23))))
    if not vat.is_finite() or not 0 <= vat <= 100:
        raise ValueError("Invalid supplier VAT setting")
    factor = 1 + vat / 100
    def money(value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    tree = etree.parse(str(path), etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
    if tree.docinfo.doctype or tree.getroot().tag not in ("products", "root"):
        raise ValueError("Expected Northfinder products XML without a DTD")
    records: OrderedDict[str, list[tuple[CatalogProduct, dict]]] = OrderedDict()
    for parent in tree.getroot():
        if not isinstance(parent.tag, str):
            continue
        if parent.tag != tags["item_tag"]:
            raise ValueError("Unexpected Northfinder listing item")
        group = _get_text(parent, tags["sku_field"])
        name = _get_text(parent, "name_b2c") or _get_text(parent, "name")
        if not group or len(group) > 100 or not name or len(name) > 500:
            raise ValueError("Invalid Northfinder product identity")
        currency = (_get_text(parent, "currency") or cfg.get("default_currency") or "EUR").upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("Invalid Northfinder currency")
        variants = parent.findall("variants/" + tags["variant_tag"])
        context = etree.Element(parent.tag, parent.attrib)
        for child in parent:
            if child.tag != "variants":
                context.append(deepcopy(child))
        # Share immutable parent fields between siblings while parsing a large feed.
        parent_fields = _source_fields(context)
        features = [CatalogParameter(name=_get_text(a, "group"), value=_get_text(a, "value"))
                    for a in parent.findall("features/feature") if _get_text(a, "group") and _get_text(a, "value")]
        description = _get_text(parent, "description") or _get_text(parent, "description_short")
        for variant in variants or [parent]:
            code = _get_text(variant, tags["sku_field"])
            if not code or len(code) > 100:
                raise ValueError("Invalid Northfinder variant code")
            warnings: list[str] = []

            def price(tag: str, basis: str) -> Decimal | None:
                value = _number(variant, tag, warnings, price=True)
                # An explicit zero or malformed value is never replaced silently.
                if variant is not parent and not _get_text(variant, tag):
                    inherited = _number(parent, tag, warnings, price=True)
                    if inherited is not None and inherited > 0:
                        warnings.append("inherited_" + basis + "_price")
                        return inherited
                return value

            purchase = price(tags["b2b_price_field"], "purchase")
            retail = price(tags["rrp_field"], "retail")
            discount = price(tags["rrp_sale_field"], "discount")
            if retail is None or retail <= 0:
                warnings.append("missing_retail_price")
            if purchase is not None and retail is not None and retail < purchase * factor:
                warnings.append("retail_below_purchase")
            attrs = [CatalogParameter(name=_get_text(a, "group"), value=_get_text(a, "value"))
                     for a in variant.findall("attributes/attribute") if _get_text(a, "group") and _get_text(a, "value")]
            attribute_names = {a.name.casefold() for a in attrs}
            params = [a for a in features if a.name.casefold() not in attribute_names]
            images = list(dict.fromkeys(url for a in [*variant.findall("images/image"), *parent.findall("images/image")]
                                       if (url := _url(a.text or ""))))
            ean = _get_text(variant, tags["ean_field"])
            eans = [ean] if ean and ean != "0" else []
            if not eans:
                warnings.append("missing_ean")
            if not images:
                warnings.append("missing_images")
            categories = [_get_text(a, ".") for a in parent.findall("categories_b2c/category")]
            if not any(categories):
                categories = [_get_text(a, ".") for a in parent.findall("categories_b2b/category")]
            # Keep the original parent context and selected variant, not every sibling per row.
            fragment = deepcopy(context)
            fields = parent_fields
            if variants:
                etree.SubElement(fragment, "variants").append(deepcopy(variant))
                fields = {**parent_fields, "variants": [{tags["variant_tag"]: [_source_fields(variant)]}]}
            product = CatalogProduct(
                supplier=supplier, feed_key=feed_key, code=code, shop_code=canonical_supplier_sku(prefix, code),
                eans=eans, name=name, brand="NORTHFINDER",
                description=description,
                category=" | ".join(filter(None, categories)) or None,
                url=_url(_get_text(variant, "product_url") or _get_text(parent, "product_url")),
                images=images, parameters=params + attrs,
                prices=CatalogPrices(currency=currency, vat_percent=vat, purchase_net=purchase,
                    purchase_gross=money(purchase * factor) if purchase is not None else None,
                    retail_gross=retail, retail_net=money(retail / factor) if retail is not None else None,
                    discount_gross=discount, discount_net=money(discount / factor) if discount is not None else None),
                supplier_stock=_number(variant, tags["qty_field"], warnings),
                supplier_stock_raw=_get_text(variant, tags["qty_field"]) or None,
                group_code=group if variants else None, group_name=name if variants else None,
                variant_attributes=attrs if variants else [],
                variant_relationship="explicit" if variants else "not_provided",
                warnings=list(dict.fromkeys(warnings)),
            )
            xml = etree.tostring(fragment, encoding="unicode", with_tail=False)
            product.source_hash = hashlib.sha256(xml.encode()).hexdigest()
            records.setdefault(code, []).append((product, {"xml": xml, "fields": fields}))
    output = []
    for matches in records.values():
        product, raw = matches[0]
        if len(matches) > 1:
            # Do not choose an EAN or a price from competing records for the same SKU.
            product.eans = list(dict.fromkeys(ean for p, _ in matches for ean in p.eans))
            product.prices = CatalogPrices(currency=product.prices.currency, vat_percent=vat)
            product.supplier_stock = product.supplier_stock_raw = None
            product.warnings = ["duplicate_supplier_code"]
            product.import_blockers = ["duplicate_supplier_code"]
            if len({p.group_code for p, _ in matches}) > 1:
                product.group_code = product.group_name = None
                product.variant_relationship = "not_provided"
                product.variant_attributes = []
            fragment = etree.Element("conflicting_items")
            for _, item in matches:
                fragment.append(etree.fromstring(item["xml"].encode(), etree.XMLParser(resolve_entities=False, no_network=True)))
            xml = etree.tostring(fragment, encoding="unicode", with_tail=False)
            product.source_hash = hashlib.sha256(xml.encode()).hexdigest()
            raw = {"xml": xml, "fields": _source_fields(fragment)}
        output.append((product, raw))
    if not output:
        raise ValueError("The Northfinder listing feed is empty")
    return output
