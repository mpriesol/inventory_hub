"""Bounded supplier reads, sharing the catalog downloader and proven parsers."""
from collections import Counter
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
    if supplier == "sloger":
        if feed_key != "b2b":
            raise AvailabilityError("supplier_availability_source_missing", 422)
        fingerprint = hashlib.sha256(json.dumps(cfg, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        return cfg, {"type": "stock", "mode": "b2b"}, "sloger-b2b", fingerprint
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


def diagnostic_value(value):
    """Only bounded stock fields are exposed; never echo a URL or exception."""
    if value is None:
        return None
    token = str(value)
    if "://" in token:
        return "[redacted]"
    return "".join(c for c in token if ord(c) >= 32 and ord(c) != 127)[:120]


class ParsedAvailability(list):
    """Accepted observations plus rejected source rows; list-compatible callers."""
    def __init__(self, records=(), issues=(), total=None):
        super().__init__(records)
        self.issues = list(issues)
        self.total = len(self) + len(self.issues) if total is None else total

    def report(self):
        unknown = sum(item["kind"] == "unknown" for item in self.issues)
        return {"total": self.total, "accepted": len(self),
                "rejected": len(self.issues) - unknown, "unknown": unknown,
                "reasons": dict(Counter(item["code"] for item in self.issues))}


def _source_rows(path, supplier, cfg, feed_key, parser):
    is_stock = feed_key == "stock" or (cfg["feeds"]["sources"][feed_key].get("type") == "stock")
    if parser == "cyklo-zitny":
        from inventory_hub.adapters.cyklo_zitny_catalog import read_stock
        for sku, raw, error in read_stock(path):
            yield sku, raw, None, None, error
    elif parser == "schindler":
        from inventory_hub.adapters.schindler_catalog import read_stock
        for sku, internal, external in read_stock(path):
            yield sku, internal, external, None, None
    elif parser == "northfinder":
        from inventory_hub.adapters.northfinder_catalog import read_availability
        for sku, raw in read_availability(path, cfg):
            yield sku, raw, None, None, None
    elif is_stock and parser == "paul-lange":
        for item in read_feed_items(path):
            yield (_get_text(item, "ITEM_ID"), _get_text(item, "STOCK"),
                   _get_text(item, "STOCK_EXTERNAL"), _get_text(item, "DELIVERY"), None)
    elif is_stock and parser == "sk-profi-bike":
        from inventory_hub.adapters.sk_profi_bike_catalog import read_stock
        for sku, raw in read_stock(path):
            yield sku, raw, None, None, None
    else:
        for product, _raw in catalog.PARSERS[parser](path, supplier, cfg, feed_key):
            internal = product.supplier_stock_raw
            if internal is None:
                internal = (str(product.supplier_stock_min) + "+" if product.supplier_stock_min is not None
                            else product.supplier_stock)
            external = product.supplier_stock_external_raw
            if external is None:
                external = product.supplier_external_available if product.supplier_external_available is not None else product.supplier_stock_external
            code = "supplier_availability_duplicate_identity" if "duplicate_supplier_code" in product.import_blockers else None
            yield product.code, internal, external, product.availability, code


def parse(path, supplier, cfg, feed_key, parser):
    # Finish structural parsing before accepting anything. A broken XML/download is
    # not a completed source; a bad quantity in an otherwise complete feed is local.
    rows = []
    counts = Counter()
    for row in _source_rows(path, supplier, cfg, feed_key, parser):
        rows.append(row)
        if isinstance(row[0], str) and row[0]:
            counts[row[0]] += 1
        if len(rows) > 250000:
            raise AvailabilityError("supplier_availability_empty_or_oversized", 422)
    if not rows:
        raise AvailabilityError("supplier_availability_empty_or_oversized", 422)
    records, issues = [], []
    for position, (sku, internal, external, label, source_error) in enumerate(rows, 1):
        code, kind = source_error, "rejected"
        if isinstance(sku, str) and counts[sku] > 1:
            code = "supplier_availability_duplicate_identity"
        item = None
        if not code:
            try:
                item = observation(sku, internal, external, label)
                if item.available is None:
                    code, kind = "supplier_availability_unknown_quantity", "unknown"
            except AvailabilityError as error:
                code = error.code
        if code:
            issues.append({"row": position, "sku": diagnostic_value(sku),
                "stock": diagnostic_value(internal), "external_stock": diagnostic_value(external),
                "code": code, "kind": kind})
        else:
            records.append(item)
    return ParsedAvailability(records, issues, len(rows))


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
