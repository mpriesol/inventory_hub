"""Bounded supplier reads, sharing the catalog downloader and proven parsers."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from lxml import etree

from inventory_hub.adapters.pl_feed_convert import _get_text, read_feed_items
from inventory_hub.services import catalog


class AvailabilityError(Exception):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status


def configuration(supplier, feed_key):
    try:
        cfg = catalog.supplier_config(supplier)
    except catalog.CatalogError:
        raise AvailabilityError("supplier_availability_source_missing", 404) from None
    source = (cfg.get("feeds", {}).get("sources") or {}).get(feed_key)
    if not isinstance(source, dict):
        raise AvailabilityError("supplier_availability_source_missing", 422)
    parser = source.get("catalog_parser") or (cfg.get("adapter_settings", {}).get("catalog") or {}).get("parser")
    parser = parser or (supplier if supplier in catalog.PARSERS else None)
    if parser not in catalog.PARSERS:
        raise AvailabilityError("supplier_availability_parser_unavailable", 422)
    if source.get("mode", "remote") == "local":
        configured = bool(source.get("local_path"))
    else:
        configured = bool((source.get("remote") or {}).get("url"))
    if not configured:
        raise AvailabilityError("supplier_availability_source_missing", 422)
    # This fingerprint is persisted, never the configured URL or credentials.
    fingerprint = hashlib.sha256(json.dumps(cfg, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    return cfg, source, parser, fingerprint


@dataclass(frozen=True)
class Observation:
    sku: str
    available: bool | None
    quantity: Decimal | None
    quantity_kind: str
    raw: dict


def quantity(value):
    """Keep exact/minimum/boolean distinct; unrecognised data never means zero."""
    if value is None or value == "":
        return None, None, "unknown"
    if isinstance(value, bool):
        return value, None, "boolean"
    token = str(value).strip().casefold()
    flags = {"ano": True, "áno": True, "yes": True, "true": True,
             "nie": False, "no": False, "false": False}
    if token in flags:
        return flags[token], None, "boolean"
    minimum = bool(re.fullmatch(r"\d+(?:[.,]\d{1,3})?\+", token))
    try:
        number = Decimal(token.rstrip("+").replace(",", "."))
        if (not number.is_finite() or number < 0 or number >= 1_000_000_000
                or number != number.quantize(Decimal("0.001")) or ("+" in token and not minimum)):
            raise InvalidOperation
    except (ValueError, InvalidOperation):
        raise AvailabilityError("supplier_availability_invalid_quantity", 422) from None
    # 0+ proves a lower bound, not availability.
    return (True if number > 0 else None if minimum else False), number, "minimum" if minimum else "exact"


def observation(sku, internal, external=None, label=None):
    if not isinstance(sku, str) or not sku.strip() or sku != sku.strip() or len(sku) > 100:
        raise AvailabilityError("supplier_availability_invalid_identity", 422)
    if any(ord(c) < 32 or ord(c) == 127 for c in sku):
        raise AvailabilityError("supplier_availability_invalid_identity", 422)
    available, count, kind = quantity(internal)
    ext_available, ext_count, ext_kind = quantity(external)
    # A supplier's external warehouse can prove orderability, but is never own stock.
    if available is not True and ext_available is True:
        available, count, kind = ext_available, ext_count, ext_kind
    elif available is None and ext_available is False:
        # A negative external flag alone does not prove the internal stock is zero.
        available, count, kind = None, None, "unknown"
    return Observation(sku, available, count, kind,
        {"stock": str(internal) if internal is not None else None,
         "external_stock": str(external) if external is not None else None,
         "availability": str(label)[:255] if label else None})


def parse(path, supplier, cfg, feed_key, parser):
    records = []
    is_stock = feed_key == "stock" or (cfg["feeds"]["sources"][feed_key].get("type") == "stock")
    if is_stock and parser == "paul-lange":
        for item in read_feed_items(path):
            records.append(observation(_get_text(item, "ITEM_ID"), _get_text(item, "STOCK"),
                                       _get_text(item, "STOCK_EXTERNAL"), _get_text(item, "DELIVERY")))
    else:
        for product, _raw in catalog.PARSERS[parser](path, supplier, cfg, feed_key):
            if "duplicate_supplier_code" in product.import_blockers:
                raise AvailabilityError("supplier_availability_duplicate_identity", 422)
            internal = product.supplier_stock_raw
            if internal is None:
                internal = (str(product.supplier_stock_min) + "+" if product.supplier_stock_min is not None
                            else product.supplier_stock)
            external = product.supplier_stock_external_raw
            if external is None:
                external = product.supplier_external_available if product.supplier_external_available is not None else product.supplier_stock_external
            records.append(observation(product.code, internal, external, product.availability))
    if not records or len(records) > 250000:
        raise AvailabilityError("supplier_availability_empty_or_oversized", 422)
    if len({row.sku for row in records}) != len(records):
        raise AvailabilityError("supplier_availability_duplicate_identity", 422)
    if not any(row.available is not None for row in records):
        raise AvailabilityError("supplier_availability_no_quantities", 422)
    return records


def fetch(plan):
    try:
        path = catalog._download(plan["supplier"], plan["source"], plan["feed_key"], max_elapsed_seconds=180)
        return parse(path, plan["supplier"], plan["config"], plan["feed_key"], plan["parser"]), path.stat().st_size
    except AvailabilityError:
        raise
    except catalog.CatalogError as error:
        raise AvailabilityError(error.code, error.status) from None
    except (ValueError, etree.LxmlError):
        raise AvailabilityError("supplier_availability_parse_failed", 422) from None
