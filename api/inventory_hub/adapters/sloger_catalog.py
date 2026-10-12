"""Sloger listing XML: explicit options, EUR DMOC and unknown acquisition costs."""
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


def _option_attributes(options) -> list[list[CatalogParameter]]:
    """Use an explicitly named source axis only when the whole family agrees."""
    labels = [_get_text(option, "name") for option in options]
    axes = re.compile(r"^(Veľkosť|Farba|Dĺžka|Šírka|Priemer|Objem|Hmotnosť|Rozmer)(?:\s*:\s*|\s+)(.+)$", re.IGNORECASE)
    matches = [axes.fullmatch(label) for label in labels]
    named = bool(matches) and all(matches) and len({match[1].casefold() for match in matches if match}) == 1
    return [[CatalogParameter(name=match[1].capitalize(), value=match[2].strip())] if named and match
            else [CatalogParameter(name="Variant", value=label)] if label else []
            for label, match in zip(labels, matches)]


def parse_catalog(path: Path, supplier: str, cfg: dict, feed_key: str = "products") -> list[tuple[CatalogProduct, dict]]:
    adapter = cfg.get("adapter_settings") or {}
    prefix = get_supplier_prefix(cfg)
    # The supplier explicitly labels its second amount price_msrp_czk. The plain
    # price_msrp is EUR; never relabel it using an unrelated invoice currency.
    vat = Decimal(str(adapter.get("vat_rate", adapter.get("vat", cfg.get("vat_rate", 23)))))
    if not vat.is_finite() or not 0 <= vat <= 100:
        raise ValueError("Invalid supplier VAT setting")
    factor = 1 + vat / 100
    tree = etree.parse(str(path), etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
    if tree.docinfo.doctype or tree.getroot().tag != "products":
        raise ValueError("Expected Sloger products XML without a DTD")
    records: OrderedDict[str, list[tuple[CatalogProduct, dict]]] = OrderedDict()
    for parent in tree.getroot():
        if not isinstance(parent.tag, str):
            continue
        if parent.tag != "product":
            raise ValueError("Unexpected Sloger listing item")
        name = _get_text(parent, "name")
        if not name or len(name) > 500:
            raise ValueError("Invalid Sloger product identity")
        options = parent.findall("options/option")
        # Several legitimate families reuse a parent code (including frame/bike
        # pairs). Sloger's explicit product ID identifies the parent uniquely.
        group = _get_text(parent, "id") if options else None
        if options and (not group or len(group) > 100):
            raise ValueError("Invalid Sloger parent ID")
        attributes = _option_attributes(options)
        context = etree.Element(parent.tag, parent.attrib)
        for child in parent:
            if child.tag != "options":
                context.append(deepcopy(child))
        parent_fields = _source_fields(context)
        parameters = [CatalogParameter(name=_get_text(p, "param_name"), value=_get_text(p, "val"))
                      for p in parent.findall("param") if _get_text(p, "param_name") and _get_text(p, "val")]
        for index, option in enumerate(options or [parent]):
            code = _get_text(option, "code")
            if not code or len(code) > 100:
                raise ValueError("Invalid Sloger option code")
            warnings = []
            retail = _number(option, "price_msrp", warnings, price=True)
            if option is not parent and not _get_text(option, "price_msrp"):
                inherited = _number(parent, "price_msrp", warnings, price=True)
                if inherited is not None and inherited > 0:
                    retail = inherited
                    warnings.append("inherited_retail_price")
            if retail is None or retail <= 0:
                warnings.append("missing_retail_price")
            # Parent EANs never identify an option. All-zero values are supplier
            # placeholders, not a shared barcode that should match other items.
            ean = _get_text(option, "ean")
            eans = [ean] if ean and set(ean) != {"0"} else []
            if not eans:
                warnings.append("missing_ean")
            images = list(dict.fromkeys(url for image in [*option.findall("images/url"), *parent.findall("images/url")]
                                       if (url := http_url(image.text or ""))))
            if not images:
                warnings.append("missing_images")
            attrs = attributes[index] if options else []
            axis_names = {a.name.casefold() for a in attrs}
            params = [p for p in parameters if p.name.casefold() not in axis_names]
            fragment = deepcopy(context)
            fields = parent_fields
            if options:
                etree.SubElement(fragment, "options").append(deepcopy(option))
                fields = {**parent_fields, "options": [{"option": [_source_fields(option)]}]}
            product = CatalogProduct(
                supplier=supplier, feed_key=feed_key, code=code, shop_code=canonical_supplier_sku(prefix, code),
                name=name, brand=_get_text(parent, "manufacturer") or None, eans=eans,
                description=_get_text(parent, "description"), category=_get_text(parent, "category") or None,
                images=images, parameters=params + attrs,
                prices=CatalogPrices(currency="EUR", vat_percent=vat, retail_gross=retail,
                    retail_net=(retail / factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if retail is not None else None),
                group_code=group if options else None, group_name=name if options else None,
                variant_attributes=attrs, variant_relationship="explicit" if options else "not_provided",
                warnings=list(dict.fromkeys(warnings)),
            )
            xml = etree.tostring(fragment, encoding="unicode", with_tail=False)
            product.source_hash = hashlib.sha256(xml.encode()).hexdigest()
            records.setdefault(code, []).append((product, {"xml": xml, "fields": fields}))
    output = []
    for matches in records.values():
        product, raw = matches[0]
        if len(matches) > 1:
            # Keep conflicting evidence visible; do not pick a cost, barcode or
            # variant relationship from competing records for a supplier SKU.
            product.eans = list(dict.fromkeys(ean for p, _ in matches for ean in p.eans))
            product.prices = CatalogPrices(currency="EUR", vat_percent=vat)
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
        raise ValueError("The Sloger listing feed is empty")
    return output
