"""Husky SK public catalog. Wholesale quotes/stock are separate B2B evidence."""
from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib

from lxml import etree

from inventory_hub.adapters.paul_lange_catalog import _source_fields, http_url
from inventory_hub.adapters.pl_feed_convert import _get_text
from inventory_hub.catalog_types import CatalogParameter, CatalogPrices, CatalogProduct
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.supplier_prefix import canonical_supplier_sku, get_supplier_prefix


def read_export(path):
    tree = etree.parse(str(path), etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
    root = tree.getroot()
    if (tree.docinfo.doctype or root.tag != "SHOP" or not 0 < len(root) <= 100000
            or any(item.tag != "SHOPITEM" for item in root)):
        raise ValueError("Expected a complete Husky SHOP export without a DTD")
    return root


def single(item, tag, *, required=False, limit=500):
    value = _get_text(item, tag)
    if (len(item.findall(tag)) > 1 or (required and not value) or len(value) > limit
            or any(ord(c) < 32 for c in value)):
        raise ValueError("Missing or ambiguous Husky field")
    return value


def money(value):
    if not value or value.strip() in ("-", "—"):
        return None
    try:
        number = Decimal(value.replace("\xa0", "").replace(" ", "").replace(",", "."))
        if not number.is_finite() or not 0 <= number < 100000000 or number.as_tuple().exponent < -4:
            raise InvalidOperation
        return number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid Husky price") from None


def read_stock(path):
    result = {}
    for item in read_export(path):
        code = single(item, "KODVYROBCE", required=True, limit=100)
        flag = single(item, "SKLADEM", required=True).casefold()
        if code in result or flag not in ("true", "false"):
            raise ValueError("Invalid Husky stock identity or flag")
        result[code] = flag == "true"
    return result


def parse_catalog(path, supplier, cfg, feed_key="products"):
    if feed_key == "stock":
        raise ValueError("The Husky stock export is not a product catalog")
    adapter = cfg.get("adapter_settings") or {}
    vat = Decimal(str(adapter["vat_rate"])) if adapter.get("vat_rate") is not None else None
    if vat is not None and (not vat.is_finite() or not 0 <= vat <= 100):
        raise ValueError("Invalid configured Husky destination VAT")
    if adapter.get("currency", "EUR") != "EUR":
        raise ValueError("Husky SK prices require EUR")
    output, seen = [], set()
    for item in read_export(path):
        code = single(item, "CODE", required=True, limit=100)
        if code in seen:
            raise ValueError("Duplicate Husky supplier code")
        seen.add(code)
        mpn = single(item, "PRODUCTNO", limit=100) or None
        group = single(item, "MASTER_PRODUCT_ID", limit=100) or None
        warnings = ["supplier_stock_unverified"]
        ean_values = list(dict.fromkeys((node.text or "").strip() for node in item.findall("EAN") if (node.text or "").strip()))
        eans = [value for value in ean_values if ProductIdentifierService.gtin_key(value)]
        if len(eans) != len(ean_values):
            warnings.append("invalid_ean")
        if not eans:
            warnings.append("missing_ean")
        retail = money(single(item, "COMMON_PRICE_VAT"))
        if retail is None:
            retail = money(single(item, "PRICE_VAT"))
        prices = CatalogPrices(currency="EUR", vat_percent=vat,
            retail_gross=retail,
            discount_gross=money(single(item, "PRICE_VAT")))
        if prices.retail_gross is None:
            warnings.append("missing_retail_price")
        if vat is None:
            warnings.append("missing_vat")
        # The historical VAT/VALUE=20 in this export is not current destination VAT.
        axes = [CatalogParameter(name=_get_text(p, "PARAM_NAME"), value=_get_text(p, "VAL"))
                for p in item.findall("PARAM") if _get_text(p, "PARAM_NAME") and _get_text(p, "VAL")]
        images = list(dict.fromkeys(url for image in item.findall("IMAGES/IMAGE")
            if (url := http_url(_get_text(image, "DOWNLOAD_FROM") or _get_text(image, "URL")))))
        if not images:
            images = list(dict.fromkeys(url for tag in ("IMGURL", "IMGURL_ALTERNATIVE")
                for node in item.findall(tag) if (url := http_url(node.text))))
        if not images:
            warnings.append("missing_images")
        params = []
        for attribute in item.findall("ATTRIBUTES/ATTRIBUTE"):
            name = _get_text(attribute, "NAME")
            values = [_get_text(v, "VALUE") for v in attribute.findall("ATTRIBUTE_ITEMS/ATTRIBUTE_ITEM")]
            if name and any(values):
                params.append(CatalogParameter(name=name, value="; ".join(v for v in values if v)))
        name = single(item, "PRODUCTNAME", required=True)
        product = CatalogProduct(supplier=supplier, feed_key=feed_key, code=code,
            shop_code=canonical_supplier_sku(get_supplier_prefix(cfg), code), manufacturer_code=mpn,
            name=name, brand=single(item, "PRODUCER") or None, eans=eans, images=images,
            description=_get_text(item, "DESCRIPTION"), short_description=_get_text(item, "ANNOTATION"),
            url=http_url(_get_text(item, "URL")), category=_get_text(item, "CATEGORYTEXT") or None,
            category_code=_get_text(item, "CATEGORYTEXT") or None, parameters=params,
            prices=prices, group_code=group, group_name=name if group else None,
            variant_attributes=axes if group else [], variant_relationship="explicit" if group else "not_provided",
            availability="overíme", warnings=warnings,
            metadata={"husky_productno": mpn or "", "husky_id": single(item, "ID", limit=100)})
        if not group:
            product.parameters.extend(axes)
        xml = etree.tostring(item, encoding="unicode", with_tail=False)
        product.source_hash = hashlib.sha256(xml.encode()).hexdigest()
        output.append((product, {"xml": xml, "fields": _source_fields(item)}))
    counts = Counter(ean for product, _ in output for ean in product.eans)
    for product, _ in output:
        if any(counts[ean] > 1 for ean in product.eans):
            product.warnings.append("duplicate_supplier_ean")
            product.import_blockers.append("duplicate_supplier_ean")
    return output
