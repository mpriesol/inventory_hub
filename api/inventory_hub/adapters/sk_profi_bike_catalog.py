"""SK Profi Bike XML: parent context and explicitly identified sellable options."""
from __future__ import annotations

import hashlib
import html
from collections import OrderedDict
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from bs4 import BeautifulSoup
from lxml import etree

from inventory_hub.adapters.paul_lange_catalog import _number, _source_fields, http_url
from inventory_hub.adapters.pl_feed_convert import _get_text
from inventory_hub.catalog_types import CatalogParameter, CatalogPrices, CatalogProduct
from inventory_hub.supplier_prefix import canonical_supplier_sku, get_supplier_prefix


def read_products(path: Path):
    tree = etree.parse(str(path), etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
    if tree.docinfo.doctype or tree.getroot().tag != "products":
        raise ValueError("Expected SK Profi Bike products XML without a DTD")
    for parent in tree.getroot():
        if not isinstance(parent.tag, str):
            continue
        if parent.tag != "product":
            raise ValueError("Unexpected SK Profi Bike feed item")
        containers = parent.findall("options")
        if len(containers) != 1 or any(isinstance(child.tag, str) and child.tag != "option" for child in containers[0]):
            raise ValueError("Invalid SK Profi Bike option structure")
        yield parent


def read_stock(path: Path):
    """Skip anonymous stock options rather than inventing a supplier SKU."""
    for parent in read_products(path):
        for option in parent.findall("options/option"):
            if len(option.findall("kod")) > 1 or len(option.findall("sklad")) > 1:
                raise ValueError("Ambiguous SK Profi Bike stock item")
            code = _get_text(option, "kod")
            if code:
                yield code, _get_text(option, "sklad") or None


def _parameters(parent):
    result = []
    for param in parent.findall("params/param"):
        name = _get_text(param, "param_name")
        value = BeautifulSoup(_get_text(param, "param_value"), "html.parser").get_text(" ", strip=True)
        if name and value:
            result.append(CatalogParameter(name=name, value=value))
    return result


def _images(parent, option):
    """Shared pictures plus this option's pictures, never a sibling colour/size."""
    result = []
    for picture in parent.findall("pictures/picture"):
        if any(picture.get(axis) and picture.get(axis) != option.get(axis) for axis in ("atribute_1", "atribute_2")):
            continue
        url = http_url(picture.text or "")
        if url and url not in result:
            result.append(url)
    return result


def parse_catalog(path: Path, supplier: str, cfg: dict, feed_key: str = "products") -> list[tuple[CatalogProduct, dict]]:
    adapter = cfg.get("adapter_settings") or {}
    prefix = get_supplier_prefix(cfg)
    configured_vat = adapter.get("vat_rate", adapter.get("vat", cfg.get("vat_rate")))
    vat = Decimal(str(configured_vat)) if configured_vat is not None else None
    if vat is not None and (not vat.is_finite() or not 0 <= vat <= 100):
        raise ValueError("Invalid supplier VAT setting")
    factor = 1 + vat / 100 if vat is not None else None

    def money(value):
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    records: OrderedDict[str, list[tuple[CatalogProduct, dict]]] = OrderedDict()
    for parent in read_products(path):
        options = parent.findall("options/option")
        # A parent with no option has no sellable SKU; its internal ID is not one.
        if not options:
            continue
        name = _get_text(parent, "name")
        group = _get_text(parent, "id")
        if not name or len(name) > 500 or not group or len(group) > 100:
            raise ValueError("Invalid SK Profi Bike product identity")
        axes = [(key, _get_text(parent, key)) for key in ("atribute_1", "atribute_2")]
        grouped = len(options) > 1 or any(label for _, label in axes)
        context = etree.Element(parent.tag, parent.attrib)
        for child in parent:
            if child.tag != "options":
                context.append(deepcopy(child))
        parent_fields = _source_fields(context)
        parameters = _parameters(parent)
        category = parent.find("category")
        for option in options:
            code = _get_text(option, "kod")
            if not code or len(code) > 100:
                raise ValueError("Invalid SK Profi Bike option code")
            warnings = [] if vat is not None else ["missing_vat"]
            retail = _number(option, "moc", warnings, price=True)
            discount = _number(option, "akcia", warnings, price=True)
            # An explicitly supplied promotional VOC is the current net offer.
            # Invalid/zero values must not silently fall back to the regular VOC.
            purchase_tag = "voc_akcia" if _get_text(option, "voc_akcia") else "voc"
            purchase = _number(option, purchase_tag, warnings, price=True)
            if retail is None or retail <= 0:
                warnings.append("missing_retail_price")
            if purchase is not None and retail is not None and factor is not None and retail < purchase * factor:
                warnings.append("retail_below_purchase")
            ean = _get_text(option, "ean")
            eans = [ean] if ean and set(ean) != {"0"} else []
            if not eans:
                warnings.append("missing_ean")
            attrs = [CatalogParameter(name=label, value=option.get(key, "").strip())
                     for key, label in axes if label and option.get(key, "").strip()]
            if grouped and (not attrs or any(label and not option.get(key, "").strip() for key, label in axes)):
                warnings.append("missing_variant_attributes")
            axis_names = {item.name.casefold() for item in attrs}
            images = _images(parent, option)
            if not images:
                warnings.append("missing_images")
            stock_raw = _get_text(option, "sklad") or None
            stock = _number(option, "sklad", warnings)
            if stock is not None and (stock < 0 or stock >= 1_000_000_000 or stock != stock.quantize(Decimal("0.001"))):
                stock = None
                warnings.append("invalid_number:sklad")
            # Keep malformed source stock in raw XML, not as a usable quantity.
            if stock is None:
                stock_raw = None
            fragment = deepcopy(context)
            etree.SubElement(fragment, "options").append(deepcopy(option))
            xml = etree.tostring(fragment, encoding="unicode", with_tail=False)
            product = CatalogProduct(
                supplier=supplier, feed_key=feed_key, code=code, shop_code=canonical_supplier_sku(prefix, code),
                name=name, short_description=_get_text(parent, "label"), brand=_get_text(parent, "manufacturer") or None,
                eans=eans, description=_get_text(parent, "description_formatted") or _get_text(parent, "description"),
                category=html.unescape(_get_text(parent, "category_tree")) or _get_text(parent, "category") or None,
                category_code=category.get("id") if category is not None else None,
                url=http_url(_get_text(parent, "url")), images=images,
                parameters=[item for item in parameters if item.name.casefold() not in axis_names] + attrs,
                prices=CatalogPrices(currency="EUR", vat_percent=vat, purchase_net=purchase,
                    purchase_gross=money(purchase * factor) if purchase is not None and factor is not None else None,
                    retail_gross=retail, retail_net=money(retail / factor) if retail is not None and factor is not None else None,
                    discount_gross=discount, discount_net=money(discount / factor) if discount is not None and factor is not None else None),
                supplier_stock=stock, supplier_stock_raw=stock_raw,
                group_code=group if grouped else None, group_name=name if grouped else None,
                variant_attributes=attrs if grouped else [], variant_relationship="explicit" if grouped else "not_provided",
                warnings=list(dict.fromkeys(warnings)), source_hash=hashlib.sha256(xml.encode()).hexdigest(),
            )
            records.setdefault(code, []).append((product, {"xml": xml,
                "fields": {**parent_fields, "options": [{"option": [_source_fields(option)]}]}}))
    output = []
    for matches in records.values():
        product, raw = matches[0]
        if len(matches) > 1:
            # Contradicting supplier identities are reviewable, never importable.
            product.eans = list(dict.fromkeys(ean for item, _ in matches for ean in item.eans))
            product.prices = CatalogPrices(currency="EUR", vat_percent=vat)
            product.supplier_stock = product.supplier_stock_raw = None
            product.warnings = product.import_blockers = ["duplicate_supplier_code"]
            if len({item.group_code for item, _ in matches}) > 1:
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
        raise ValueError("The SK Profi Bike listing feed contains no sellable options")
    return output
