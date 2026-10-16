"""Bike Brothers SHOP export: leaf SKUs, explicit families and EUR net prices."""
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import os

from lxml import etree

from inventory_hub.adapters.paul_lange_catalog import _number, _source_fields, http_url
from inventory_hub.adapters.pl_feed_convert import _get_text
from inventory_hub.catalog_types import CatalogParameter, CatalogPrices, CatalogProduct
from inventory_hub.supplier_prefix import canonical_supplier_sku, get_supplier_prefix


def single(item, tag, *, required=False, limit=None):
    value = _get_text(item, tag)
    if (len(item.findall(tag)) > 1 or (required and not value)
            or (limit and len(value) > limit) or any(ord(c) < 32 for c in value)):
        raise ValueError("Missing or ambiguous Bike Brothers identity")
    return value


def read_export(path):
    tree = etree.parse(str(path), etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
    root = tree.getroot()
    if (tree.docinfo.doctype or root.tag != "SHOP" or not len(root) or len(root) > 100000
            or any(item.tag != "SHOPITEM" for item in root)):
        raise ValueError("Expected a complete Bike Brothers SHOP export without a DTD")
    by_id, parents = {}, set()
    for item in root:
        identity = single(item, "id", required=True, limit=100)
        if identity in by_id:
            raise ValueError("Duplicate Bike Brothers internal ID")
        by_id[identity] = item
        group = single(item, "mastergroup", limit=100)
        if group:
            parents.add(group)
    if any(single(by_id[key], "mastergroup") for key in parents if key in by_id):
        raise ValueError("Nested or cyclic Bike Brothers families are unsupported")
    # Parent stock is the aggregate of its children, never another sellable SKU.
    leaves = [item for key, item in by_id.items() if key not in parents]
    if not leaves:
        raise ValueError("The Bike Brothers export has no leaf products")
    return leaves, by_id


def inspection_categories(path):
    leaves, by_id = read_export(path)
    counts = Counter()
    for item in leaves:
        parent = by_id.get(_get_text(item, "mastergroup"))
        category = _get_text(item, "kategorie") or (_get_text(parent, "kategorie") if parent is not None else "")
        if category:
            counts[category] += 1
    return {"source_categories": sorted(counts), "source_category_options": [
        {"source": label, "code": label, "label": label, "path": label, "count": counts[label]}
        for label in sorted(counts)], "category_fields": {}}


def read_stock(path):
    leaves, _ = read_export(path)
    for item in leaves:
        error = ("supplier_availability_invalid_identity" if len(item.findall("kod")) != 1 else
                 "supplier_availability_invalid_quantity" if len(item.findall("stav_zasob")) > 1 else None)
        yield _get_text(item, "kod"), _get_text(item, "stav_zasob") or None, error


def parse_catalog(path, supplier, cfg, feed_key="products"):
    leaves, by_id = read_export(path)
    adapter = cfg.get("adapter_settings") or {}
    language = (adapter.get("catalog") or {}).get("source_language", "cs")
    if language not in ("cs", "sk"):
        raise ValueError("Configure the verified Bike Brothers source language as cs or sk")
    try:
        value = adapter.get("vat_rate")
        vat = Decimal(str(value)) if value is not None else None
        if vat is not None and (not vat.is_finite() or not 0 <= vat <= 100):
            raise InvalidOperation
    except InvalidOperation:
        raise ValueError("Invalid configured Bike Brothers destination VAT") from None
    factor = 1 + vat / 100 if vat is not None else None
    money = lambda value: value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    groups = defaultdict(list)
    for item in leaves:
        groups[_get_text(item, "mastergroup")].append(item)
    ean_counts = Counter(_get_text(item, "ean") for item in leaves if _get_text(item, "ean"))
    output, seen = [], set()
    for item in leaves:
        code = single(item, "kod", required=True, limit=100)
        name = single(item, "nazev", required=True, limit=500)
        if code in seen:
            raise ValueError("Duplicate Bike Brothers supplier SKU")
        seen.add(code)
        group = single(item, "mastergroup", limit=100)
        parent = by_id.get(group) if group else None
        def inherited(tag):
            return _get_text(item, tag) or (_get_text(parent, tag) if parent is not None else "")
        warnings, blockers = (["source_language_cs"] if language == "cs" else []), []
        if vat is None:
            warnings.append("missing_vat")
        for tag in ("cena_bez_dph_eurvoc", "cena_bez_dph_eurmoc"):
            if len(item.findall(tag)) > 1:
                raise ValueError("Ambiguous Bike Brothers EUR price")
        purchase = _number(item, "cena_bez_dph_eurvoc", warnings, price=True)
        retail = _number(item, "cena_bez_dph_eurmoc", warnings, price=True)
        if retail is None or retail <= 0:
            warnings.append("missing_retail_price")
        if retail is not None and purchase is not None and retail < purchase:
            warnings.append("retail_below_purchase")
        ean = single(item, "ean", limit=100)
        if not ean:
            warnings.append("missing_ean")
        elif ean_counts[ean] > 1:
            warnings.append("duplicate_supplier_ean")
            blockers.append("duplicate_supplier_ean")
        image_item = item if any(_get_text(item, tag) for tag in ("obrazek_hlavni", "obrazek")) else parent
        images = list(dict.fromkeys(url for tag in ("obrazek_hlavni", "obrazek")
                                   for node in image_item.findall(tag) if (url := http_url(node.text)))) if image_item is not None else []
        if not images:
            warnings.append("missing_images")
        category = inherited("kategorie") or None
        if not category:
            warnings.append("missing_category")
        raw_stock = _get_text(item, "stav_zasob") or None
        from inventory_hub.services.supplier_availability_source import quantity, AvailabilityError
        try:
            if len(item.findall("stav_zasob")) > 1:
                raise AvailabilityError("supplier_availability_invalid_quantity")
            available, stock, kind = quantity(raw_stock)
        except AvailabilityError:
            available, stock, kind = None, None, "unknown"
            warnings.append("invalid_number:stav_zasob")
        group_name, attributes = None, []
        if group:
            group_name = _get_text(parent, "nazev") if parent is not None else ""
            if not group_name:
                names = [_get_text(member, "nazev") for member in groups[group]]
                common = os.path.commonprefix(names)
                if common and not common[-1].isspace():
                    common = common.rsplit(" ", 1)[0] if " " in common else ""
                group_name = common.strip(" -/(") or name
            if len(groups[group]) > 1:
                # The source gives titles, not structured size/colour axes. Keep
                # the exact title as a neutral choice; never parse guessed axes.
                attributes = [CatalogParameter(name="Variant", value=name)]
                warnings.append("variant_label_from_title")
        xml = etree.tostring(item, encoding="unicode", with_tail=False)
        parent_xml = etree.tostring(parent, encoding="unicode", with_tail=False) if parent is not None else None
        product = CatalogProduct(supplier=supplier, feed_key=feed_key, code=code,
            shop_code=canonical_supplier_sku(get_supplier_prefix(cfg), code), name=name,
            brand=inherited("vyrobce") or None, eans=[ean] if ean else [], images=images,
            description="\n".join(dict.fromkeys(inherited(tag) for tag in ("popis_1", "popis_2", "popis_3") if inherited(tag))),
            category=category, category_code=category,
            prices=CatalogPrices(currency="EUR", vat_percent=vat, vat_source="configured",
                purchase_net=purchase, retail_net=retail,
                purchase_gross=money(purchase * factor) if purchase is not None and factor is not None else None,
                retail_gross=money(retail * factor) if retail is not None and factor is not None else None),
            supplier_available=available, supplier_stock=stock if kind == "exact" else None,
            supplier_stock_min=stock if kind == "minimum" else None, supplier_stock_raw=raw_stock,
            group_code=group or None, group_name=group_name,
            variant_relationship="explicit" if group else "not_provided", variant_attributes=attributes,
            warnings=warnings, import_blockers=blockers,
            source_hash=hashlib.sha256((xml + (parent_xml or "") + language).encode()).hexdigest())
        output.append((product, {"xml": xml, "fields": _source_fields(item),
                                 "parent_xml": parent_xml, "source_language": language}))
    from inventory_hub.services.catalog_variants import normalize_singleton_groups
    normalize_singleton_groups([p for p, _ in output])
    return output
