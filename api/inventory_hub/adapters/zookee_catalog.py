"""Zookee's ITEMS catalog and independent export stock feed.

Source prices are Czech retail prices, never acquisition prices or EUR.
Only explicit ITEMGROUP_ID / VARIABLE_PARAMS define variant families.
"""
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
import hashlib

from lxml import etree

from inventory_hub.adapters.paul_lange_catalog import _number, _source_fields, http_url
from inventory_hub.adapters.pl_feed_convert import _get_text
from inventory_hub.catalog_types import CatalogParameter, CatalogPrices, CatalogProduct
from inventory_hub.supplier_prefix import canonical_supplier_sku, get_supplier_prefix


def read_export(path, root_tag="ITEMS", item_tag="ITEM"):
    tree = etree.parse(str(path), etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
    root = tree.getroot()
    if (tree.docinfo.doctype or root.tag != root_tag or not 0 < len(root) <= 100000
            or any(item.tag != item_tag for item in root)):
        raise ValueError("Expected a complete Zookee export without a DTD")
    return list(root)


def single(item, tag, *, required=False):
    value = _get_text(item, tag)
    if len(item.findall(tag)) > 1 or (required and not value) or any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        raise ValueError("Missing or ambiguous Zookee field: " + tag)
    return value


def inspection_categories(path):
    items = read_export(path)
    counts = Counter(single(item, "CATEGORY_ID") for item in items if single(item, "CATEGORY_ID"))
    labels = {single(item, "CATEGORY_ID"): single(item, "CATEGORYTEXT") for item in items}
    return {"source_categories": sorted(counts), "source_category_options": [
        {"source": code, "code": code, "label": labels[code] or code,
         "path": labels[code] or code, "count": counts[code]} for code in sorted(counts)], "category_fields": {}}


def read_stock(path):
    for item in read_export(path, "export", "item"):
        error = ("supplier_availability_invalid_identity" if len(item.findall("code")) != 1 else
                 "supplier_availability_invalid_quantity" if len(item.findall("inStock")) > 1 else None)
        yield _get_text(item, "code"), _get_text(item, "inStock") or None, error


def parse_catalog(path, supplier, cfg, feed_key="products", *, stock_path=None):
    items = read_export(path)
    codes = Counter(single(item, "kod") for item in items)
    eans = Counter(single(item, "BARCODE") for item in items if single(item, "BARCODE"))
    missing = codes.get("", 0)
    output = []
    for item in items:
        code = single(item, "kod")
        # The source contains uncoded rows. Never invent a supplier identity.
        if not code:
            continue
        if len(code) > 100 or codes[code] != 1 or any(ord(c) < 32 for c in code):
            raise ValueError("Duplicate or oversized Zookee supplier SKU")
        warnings, blockers = ["source_language_cs", "missing_purchase_price"], []
        if missing:
            warnings.append("source_rows_without_code:" + str(missing))
        name = single(item, "TITLE", required=True)
        ean = single(item, "BARCODE")
        if not ean:
            warnings.append("missing_ean")
        elif eans[ean] > 1:
            warnings.append("duplicate_supplier_ean")
            blockers.append("duplicate_supplier_ean")
        vat = _number(item, "VAT", warnings, price=True)
        if vat is not None and not 0 <= vat <= 100:
            raise ValueError("Invalid Zookee source VAT")
        retail = _number(item, "RRP", warnings, price=True)
        current = _number(item, "PRICE", warnings, price=True)
        factor = 1 + vat / 100 if vat is not None else None
        net = lambda value: (value / factor).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) if value is not None and factor else None
        params = [CatalogParameter(name=single(p, "NAME", required=True), value=single(p, "VALUE", required=True))
                  for p in item.findall("PARAM") if _get_text(p, "VALUE")]
        group = single(item, "ITEMGROUP_ID") or None
        axes = [str(value).strip() for value in item.xpath("VARIABLE_PARAMS/PARAM/text()")]
        attributes = []
        for axis in axes:
            values = [p for p in params if p.name == axis]
            if len(values) != 1:
                blockers.append("variant_attributes_missing")
            else:
                attributes.extend(values)
        if group and not axes:
            blockers.append("variant_attributes_missing")
        images = list(dict.fromkeys(url for media in item.findall("MEDIA") if (url := http_url(_get_text(media, "URL")))))
        if not images:
            warnings.append("missing_images")
        from inventory_hub.services.supplier_availability_source import quantity, AvailabilityError
        raw_stock = single(item, "sklad") or None
        try:
            available, stock, kind = quantity(raw_stock)
        except AvailabilityError:
            available, stock, kind = None, None, "unknown"
            warnings.append("invalid_number:sklad")
        xml = etree.tostring(item, encoding="unicode", with_tail=False)
        product = CatalogProduct(supplier=supplier, feed_key=feed_key, code=code,
            shop_code=canonical_supplier_sku(get_supplier_prefix(cfg), code), name=name,
            manufacturer_code=code, brand=single(item, "BRAND_ID") or None, eans=[ean] if ean else [],
            description=single(item, "LONGDESC"), short_description=single(item, "SHORTDESC"),
            category=single(item, "CATEGORYTEXT") or None, category_code=single(item, "CATEGORY_ID") or None,
            images=images, parameters=params,
            prices=CatalogPrices(currency="CZK", vat_percent=vat, vat_source="feed",
                retail_gross=retail, retail_net=net(retail),
                discount_gross=current if current is not None and current != retail else None,
                discount_net=net(current) if current is not None and current != retail else None),
            supplier_available=available, supplier_stock=stock if kind == "exact" else None,
            supplier_stock_min=stock if kind == "minimum" else None, supplier_stock_raw=raw_stock,
            group_code=group, group_name=single(item, "ITEMGROUP_TITLE") or None,
            variant_relationship="explicit" if group else "not_provided", variant_attributes=attributes,
            warnings=warnings, import_blockers=blockers, source_hash=hashlib.sha256(xml.encode()).hexdigest())
        output.append((product, {"xml": xml, "fields": _source_fields(item), "source_language": "cs"}))
    if not output:
        raise ValueError("The Zookee catalog has no valid supplier SKUs")
    from inventory_hub.services.catalog_variants import normalize_singleton_groups
    normalize_singleton_groups([p for p, _ in output])
    if stock_path is not None:
        # Feed 5 contains older/unavailable SKUs absent from the rich catalog.
        # Retain their exact identity for supplier linking, with an explicit
        # content warning. Never infer their variant family from the title.
        known = {p.code for p, _ in output}
        stock_items = read_export(stock_path, "export", "item")
        stock_codes = Counter(single(item, "code") for item in stock_items)
        all_eans = Counter(single(item, "ean") for item in stock_items if single(item, "ean"))
        for item in stock_items:
            code = single(item, "code")
            if not code or code in known:
                continue
            if stock_codes[code] > 1 or len(code) > 100:
                raise ValueError("Duplicate Zookee stock supplier SKU")
            warnings = ["stock_feed_only", "missing_purchase_price", "source_language_cs"]
            ean = single(item, "ean")
            blockers = ["duplicate_supplier_ean"] if ean and all_eans[ean] > 1 else []
            if blockers: warnings.extend(blockers)
            xml = etree.tostring(item, encoding="unicode", with_tail=False)
            product = CatalogProduct(supplier=supplier, feed_key=feed_key, code=code,
                shop_code=canonical_supplier_sku(get_supplier_prefix(cfg), code), manufacturer_code=code,
                name=single(item, "title", required=True), brand=single(item, "brand") or None,
                eans=[ean] if ean else [], prices=CatalogPrices(currency="CZK"),
                warnings=warnings, import_blockers=blockers, source_hash=hashlib.sha256(xml.encode()).hexdigest())
            output.append((product, {"xml":xml, "fields":_source_fields(item), "source_language":"cs"}))
    return output
