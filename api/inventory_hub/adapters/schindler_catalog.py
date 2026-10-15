"""Schindler's documented B2B SHOP feed; supplier stock is never own stock."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import os
from pathlib import Path

from lxml import etree

from inventory_hub.adapters.paul_lange_catalog import _number, _source_fields, http_url
from inventory_hub.adapters.pl_feed_convert import _get_text, read_feed_items
from inventory_hub.catalog_types import CatalogParameter, CatalogPrices, CatalogProduct
from inventory_hub.supplier_prefix import canonical_supplier_sku, get_supplier_prefix


def _warehouse_quantity(warehouse):
    raw = _get_text(warehouse, "QUANTITY/VALUE")
    greater = _get_text(warehouse, "QUANTITY/IS_GREATER_THAN").casefold()
    try:
        value = Decimal(raw)
        if (not value.is_finite() or value < 0 or value >= 999999999
                or value != value.to_integral_value() or greater not in ("true", "false")):
            raise InvalidOperation
    except InvalidOperation:
        return None, False
    # Whole pieces: >10 proves a lower bound of 11, not exactly 10.
    return value + (1 if greater == "true" else 0), greater == "true"


def stock_values(item):
    warehouses = item.findall("WAREHOUSES/WAREHOUSE")
    if not warehouses:
        return _get_text(item, "STOCK_ITEM") or None, None
    values = {"own": [], "external": []}
    for warehouse in warehouses:
        name = _get_text(warehouse, "WAREHOUSE_NAME")
        if name not in ("central", "internal", "external"):
            return "invalid_warehouse", None
        quantity, minimum = _warehouse_quantity(warehouse)
        if quantity is None:
            return "invalid_quantity", None
        values["external" if name == "external" else "own"].append((quantity, minimum))
    def total(rows):
        if not rows:
            return None
        value = str(int(sum(row[0] for row in rows)))
        return value + "+" if any(row[1] for row in rows) else value
    # WAREHOUSES lists only warehouses with stock; STOCK_ITEM explicitly
    # records the supplier's immediate stock, including a genuine zero.
    return total(values["own"]) or _get_text(item, "STOCK_ITEM") or None, total(values["external"])


def read_stock(path):
    for item in read_feed_items(path):
        internal, external = stock_values(item)
        yield _get_text(item, "CODE"), internal, external


def parse_catalog(path: Path, supplier: str, cfg: dict, feed_key="products"):
    prefix = get_supplier_prefix(cfg)
    currency = str((cfg.get("adapter_settings") or {}).get("currency") or "EUR").upper()
    output, seen, groups = [], set(), defaultdict(list)
    for item in read_feed_items(path):
        code, name = _get_text(item, "CODE"), _get_text(item, "PRODUCTNAME")
        if not code or len(code) > 100 or not name or len(name) > 500 or code in seen:
            raise ValueError("Invalid or duplicate Schindler product identity")
        seen.add(code)
        warnings = []
        vat = _number(item, "VAT", warnings, price=True)
        if vat is not None and vat > 100:
            vat = None
            warnings.append("invalid_price_vat")
        if vat is None:
            warnings.append("missing_vat")
        gross = {basis: _number(item, tag, warnings, price=True) for basis, tag in (
            ("purchase", "PURCHASE_PRICE"), ("retail", "CUSTOMER_PRICE"))}
        # CATALOGUE_PRICE is a separate manufacturer reference price. Keep it
        # in the source snapshot, never a discount or an arbitrary shop meta.
        prices = {"currency": currency, "vat_percent": vat, "vat_source": "feed"}
        for basis, value in gross.items():
            prices[basis + "_gross"] = value
            if value is not None and vat is not None:
                prices[basis + "_net"] = (value / (1 + vat / 100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if gross["retail"] is None or gross["retail"] <= 0:
            warnings.append("missing_retail_price")
        if gross["purchase"] is not None and gross["retail"] is not None and gross["retail"] < gross["purchase"]:
            warnings.append("retail_below_purchase")
        params = [CatalogParameter(name=_get_text(p, "PARAM_NAME"), value=_get_text(p, "VAL"))
                  for p in item.findall("PARAM") if _get_text(p, "PARAM_NAME") and _get_text(p, "VAL")]
        eans = list(dict.fromkeys(e.text.strip() for e in item.findall("EAN") if e.text and e.text.strip()))
        images = list(dict.fromkeys(url for e in [*item.findall("IMGURL"), *item.findall("IMGURL_ALTERNATIVE")]
                                    if (url := http_url(e.text))))
        if not eans:
            warnings.append("missing_ean")
        if not images:
            warnings.append("missing_images")
        group, variant = _get_text(item, "ITEMGROUP_ID") or None, _get_text(item, "VARIANT")
        if group and len(group) > 100:
            raise ValueError("Schindler group identity exceeds the catalog limit")
        internal, external = stock_values(item)
        from inventory_hub.services.supplier_availability_source import quantity, AvailabilityError
        try:
            available, count, kind = quantity(internal)
            external_available, external_count, external_kind = quantity(external)
        except AvailabilityError:
            available = external_available = count = external_count = None
            kind = external_kind = "unknown"
            warnings.append("invalid_number:STOCK_ITEM")
        xml = etree.tostring(item, encoding="unicode", with_tail=False)
        product = CatalogProduct(supplier=supplier, feed_key=feed_key, code=code,
            shop_code=canonical_supplier_sku(prefix, code), name=name, brand=_get_text(item, "MANUFACTURER") or None,
            eans=eans, images=images, description=_get_text(item, "DESCRIPTION"), short_description=_get_text(item, "DESCRIPTION_SHORT"),
            category=_get_text(item, "CATEGORYTEXT") or None, url=http_url(_get_text(item, "URL")),
            parameters=params, prices=CatalogPrices(**prices),
            supplier_stock=count if kind == "exact" else None, supplier_stock_min=count if kind == "minimum" else None,
            supplier_stock_raw=internal, supplier_stock_external=external_count if external_kind == "exact" else None,
            supplier_stock_external_raw=external, supplier_external_available=external_available,
            supplier_available=True if available is True or external_available is True else available,
            delivery_date=_get_text(item, "DELIVERY_DATE") or None,
            group_code=group, group_name=name if group else None,
            variant_attributes=[CatalogParameter(name="Variant", value=variant)] if group and variant else [],
            variant_relationship="explicit" if group else "not_provided", warnings=warnings,
            source_hash=hashlib.sha256(xml.encode()).hexdigest())
        if group:
            groups[group].append(product)
        output.append((product, {"xml": xml, "fields": _source_fields(item)}))
    if not output:
        raise ValueError("The Schindler product feed is empty")
    for members in groups.values():
        if len(members) < 2:
            continue
        names = [p.name for p in members]
        common = os.path.commonprefix(names)
        if common and not common[-1].isspace() and any(len(n) > len(common) and n[len(common)].isalnum() for n in names):
            common = common.rsplit(" ", 1)[0] if " " in common else ""
        family = common.strip(" -/(") or names[0]
        for product in members:
            product.group_name = family
            if not product.variant_attributes:
                product.warnings.append("missing_variant_attributes")
    from inventory_hub.services.catalog_variants import normalize_singleton_groups
    normalize_singleton_groups([p for p, _ in output])
    return output
