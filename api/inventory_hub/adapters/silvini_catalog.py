"""Silvini listing XML: explicit variants, duplicate rows and unverified stock."""
from collections import Counter
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib

from lxml import etree

from inventory_hub.adapters.paul_lange_catalog import _number, _source_fields, http_url
from inventory_hub.adapters.pl_feed_convert import _get_text
from inventory_hub.catalog_types import CatalogParameter, CatalogPrices, CatalogProduct
from inventory_hub.supplier_prefix import canonical_supplier_sku, get_supplier_prefix


def read_listing(path):
    tree = etree.parse(str(path), etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
    root = tree.getroot()
    if tree.docinfo.doctype or root.tag != "SHOP" or not len(root) or len(root) > 100000:
        raise ValueError("Expected a complete Silvini SHOP listing without a DTD")
    if any(item.tag != "SHOPITEM" for item in root):
        raise ValueError("Unexpected Silvini listing structure")
    return root


def _identity(item, tag, limit, *, required=True):
    value = _get_text(item, tag)
    if len(item.findall(tag)) > 1 or (required and not value) or len(value) > limit:
        raise ValueError("Missing or ambiguous Silvini identity")
    return value


def _context(item):
    # The supplier repeats the entire family once per size with a different
    # parent WEIGHT. It cannot be assigned to a specific variant. Keep the raw
    # weights separately; never export one arbitrarily. Images can be merged.
    return tuple((child.tag, etree.tostring(child, method="c14n")) for child in item
                 if child.tag not in ("VARIANTS", "IMAGES", "WEIGHT"))


def listing_rows(path):
    rows = {}
    count = 0
    for item in read_listing(path):
        _identity(item, "NAME", 500)
        pair = _identity(item, "PAIRCODE", 100, required=False)
        context = _context(item)
        for variant in item.findall("VARIANTS/VARIANT"):
            count += 1
            if count > 100000:
                raise ValueError("Oversized Silvini variant listing")
            code = _identity(variant, "CODE", 100)
            signature = etree.tostring(variant, method="c14n")
            images = [url for node in item.findall("IMAGES/IMAGE") if (url := http_url(node.text))]
            if code not in rows:
                rows[code] = {"item": item, "variant": variant, "pair": pair, "context": context,
                              "signature": signature, "conflict": False, "images": [], "weights": [], "count": 0}
            row = rows[code]
            row["conflict"] |= row["signature"] != signature or row["context"] != context or row["pair"] != pair
            row["images"] = list(dict.fromkeys([*row["images"], *images]))
            weight = _get_text(item, "WEIGHT")
            if weight and weight not in row["weights"]:
                row["weights"].append(weight)
            row["count"] += 1
    if not rows:
        raise ValueError("The Silvini listing has no variant SKUs")
    return rows


def category(item):
    return _get_text(item, "CATEGORIES/DEFAULT_CATEGORY") or None


def inspection_categories(path):
    counts = Counter(category(row["item"]) for row in listing_rows(path).values())
    counts.pop(None, None)
    return {"source_categories": sorted(counts), "category_fields": {},
            "source_category_options": [{"source": label, "code": label, "label": label, "path": label, "count": counts[label]}
                                        for label in sorted(counts)]}


def _flag(variant, name):
    value = _get_text(variant, name).casefold()
    if value not in ("true", "false", "1", "0"):
        raise ValueError("Invalid Silvini price discount flag")
    return value in ("true", "1")


def _prices(variant, adapter, warnings):
    settings = adapter.get("catalog") or {}
    prices = {"currency": _identity(variant, "CURRENCY", 3)}
    if prices["currency"] not in ("EUR", "CZK", "USD"):
        raise ValueError("Unsupported Silvini feed currency")
    try:
        value = adapter.get("vat_rate")
        vat = Decimal(str(value)) if value is not None else None
        if vat is not None and (not vat.is_finite() or not 0 <= vat <= 100):
            raise InvalidOperation
    except InvalidOperation:
        raise ValueError("Invalid configured Silvini destination VAT") from None
    prices["vat_percent"] = vat
    if vat is None:
        warnings.append("missing_vat")
    factor = 1 + vat / 100 if vat is not None else None
    # No VAT field in the supplied listing establishes the price basis. Only
    # explicit, independently verified operator configuration can authorize it.
    for basis, tag in (("purchase", "PURCHASE_PRICE"), ("retail", "STANDARD_PRICE")):
        policy = settings.get(basis + "_price_basis")
        if policy not in (None, "net", "gross"):
            raise ValueError("Invalid Silvini price basis")
        if policy is None:
            warnings.append("price_basis_unconfirmed")
            continue
        if basis == "purchase" and _flag(variant, "DISCOUNT_PURCHASE_YESNO"):
            tag = "DISCOUNT_PURCHASE_PRICE"
        if len(variant.findall(tag)) > 1:
            raise ValueError("Ambiguous Silvini price")
        number = _number(variant, tag, warnings, price=True)
        prices[basis + "_" + policy] = number
        if number is not None and factor is not None:
            other = number / factor if policy == "gross" else number * factor
            prices[basis + ("_net" if policy == "gross" else "_gross")] = other.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if prices.get("retail_gross") is None or prices["retail_gross"] <= 0:
        warnings.append("missing_retail_price")
    if (prices.get("retail_gross") is not None and prices.get("purchase_gross") is not None
            and prices["retail_gross"] < prices["purchase_gross"]):
        warnings.append("retail_below_purchase")
    return CatalogPrices(**prices)


def parse_catalog(path, supplier, cfg, feed_key="products"):
    source = (cfg.get("feeds", {}).get("sources") or {}).get(feed_key) or {}
    if feed_key == "stock" or source.get("type") == "stock":
        raise ValueError("Silvini update availability is not yet verified; listing zeros are not stock observations")
    adapter, prefix = cfg.get("adapter_settings") or {}, get_supplier_prefix(cfg)
    output = []
    for code, row in listing_rows(path).items():
        item, variant, pair = row["item"], row["variant"], row["pair"]
        warnings = ["supplier_stock_unverified"]
        axes = [CatalogParameter(name=_get_text(p, "NAME"), value=_get_text(p, "VALUE"))
                for p in variant.findall("PARAMETERS/PARAMETER") if _get_text(p, "NAME") and _get_text(p, "VALUE")]
        params = [CatalogParameter(name=tag, value=value) for tag in ("COMPOSITION", "GENDER", "FEATURES", "MATERIALS", "SPORT")
                  if (value := _get_text(item, tag))]
        eans = list(dict.fromkeys((node.text or "").strip() for node in variant.findall("EAN") if (node.text or "").strip()))
        if not eans:
            warnings.append("missing_ean")
        if not row["images"]:
            warnings.append("missing_images")
        if pair and not axes:
            warnings.append("missing_variant_attributes")
        blockers = ["duplicate_supplier_code"] if row["conflict"] else []
        warnings.extend(blockers)
        product = CatalogProduct(supplier=supplier, feed_key=feed_key, code=code,
            shop_code=canonical_supplier_sku(prefix, code), name=_get_text(item, "NAME"),
            brand=_get_text(item, "MANUFACTURER") or None, manufacturer_code=_get_text(variant, "EXTERNAL_CODE") or None,
            eans=eans, images=row["images"], description=_get_text(item, "DESCRIPTION"),
            short_description=_get_text(item, "SHORT_DESCRIPTION"), parameters=params if pair else params + axes,
            category=category(item), category_code=category(item), prices=_prices(variant, adapter, warnings),
            group_code=pair or None, group_name=_get_text(item, "NAME") if pair else None,
            variant_attributes=axes if pair else [], variant_relationship="explicit" if pair else "not_provided",
            warnings=list(dict.fromkeys(warnings)), import_blockers=blockers)
        # A per-SKU raw record prevents generic mapping from correlating parallel
        # size/code/price arrays or exposing sibling EANs as the current leaf.
        node = deepcopy(item)
        for old in node.findall("VARIANTS") + node.findall("WEIGHT") + node.findall("IMAGES"):
            node.remove(old)
        images = etree.SubElement(node, "IMAGES")
        for url in row["images"]:
            etree.SubElement(images, "IMAGE").text = url
        etree.SubElement(node, "VARIANTS").append(deepcopy(variant))
        xml = etree.tostring(node, encoding="unicode", with_tail=False)
        product.source_hash = hashlib.sha256((xml + str(row["conflict"])).encode()).hexdigest()
        output.append((product, {"xml": xml, "fields": _source_fields(node), "parent_weights": row["weights"],
                                 "source_row_count": row["count"]}))
    from inventory_hub.services.catalog_variants import normalize_singleton_groups
    normalize_singleton_groups([p for p, _ in output])
    return output
