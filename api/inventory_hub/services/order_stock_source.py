"""Strict, private Upgates observations for controlled local order operations.

This module never persists a source response, modifies identity, or writes to a
shop. Status semantics, cutover and stock effects belong to the caller.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from uuid import UUID

from inventory_hub.db_models import Shop
from inventory_hub.services.product_identity import RemoteIdentity, load_identity_index, verified_barcodes
from inventory_hub.services.upgates import UpgatesClient, UpgatesError


IDENTITY_FIELDS = {"classification", "product_id", "sku", "matched_by", "reasons"}


class SourceError(Exception):
    def __init__(self, code: str, status: int = 502):
        self.code, self.status = code, status
        super().__init__(code)


def _invalid():
    return SourceError("order_stock_invalid_response")


def _text(value, limit=100, *, blank=True) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise _invalid()
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise _invalid() from None
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or (not blank and not value.strip()):
        raise _invalid()
    return value.strip()


def _optional_text(value, limit=100) -> str:
    return "" if value is None else _text(value, limit)


def _positive_id(value) -> int:
    if isinstance(value, bool):
        raise _invalid()
    if isinstance(value, int) and 0 < value < 10**18:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 18 and int(value) > 0:
        return int(value)
    raise _invalid()


def _uuid(value) -> str:
    value = _text(value, 36, blank=False)
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise _invalid() from None
    if parsed.int == 0:
        raise _invalid()
    return str(parsed)


def _timestamp(value) -> str:
    value = _text(value, 40, blank=False)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _invalid() from None
    if "T" not in value or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _invalid()
    return parsed.astimezone(timezone.utc).isoformat()


def _paid_date(value) -> str | None:
    if value in (None, ""):
        return None
    value = _text(value, 40, blank=False)
    if "T" in value:
        return _timestamp(value)
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise _invalid() from None
    if parsed.isoformat() != value:
        raise _invalid()
    return value


def _quantity(value) -> str | None:
    if isinstance(value, bool) or value is None or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        number = Decimal(str(value))
        if not number.is_finite() or not 0 < number <= Decimal("999999999.999") or number != number.quantize(Decimal("0.001")):
            return None
        return format(number, "f").rstrip("0").rstrip(".") if "." in format(number, "f") else format(number, "f")
    except (InvalidOperation, ValueError):
        return None


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _statuses(payload: dict) -> dict:
    rows = payload.get("order_statuses") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > 1000:
        raise _invalid()
    public, facts, seen = [], [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise _invalid()
        identifier = _positive_id(row.get("id"))
        if identifier in seen:
            raise _invalid()
        seen.add(identifier)
        kind = _text(row.get("type"), 50, blank=False)
        descriptions = row.get("descriptions", [])
        if not isinstance(descriptions, list) or len(descriptions) > 100:
            raise _invalid()
        names = []
        for entry in descriptions:
            if not isinstance(entry, dict):
                raise _invalid()
            names.append({"language_id": _text(entry.get("language_id"), 20, blank=False),
                          "name": _text(entry.get("name"), 100)})
        names.sort(key=lambda entry: (entry["language_id"], entry["name"]))
        name = next((entry["name"] for entry in sorted(names, key=lambda entry:
                     ({"sk": 0, "cs": 1}.get(entry["language_id"], 2), entry["language_id"])) if entry["name"]), "")
        fact = {"id": identifier, "type": kind, "descriptions": names}
        for field in ("mark_resolved_yn", "mark_paid_yn", "mark_delivered_yn"):
            value = row.get(field)
            if value is not None and not isinstance(value, bool):
                raise _invalid()
            fact[field] = value
        for field in ("creation_time", "last_update_time"):
            fact[field] = _timestamp(row[field]) if row.get(field) is not None else None
        public.append({"id": identifier, "name": name, "type": kind})
        facts.append(fact)
    return {"statuses": sorted(public, key=lambda row: row["id"]),
            "status_hash": _hash(sorted(facts, key=lambda row: row["id"]))}


def _line(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise _invalid()
    invalid = False
    texts = {}
    for key in ("code", "ean"):
        try:
            texts[key] = _optional_text(raw.get(key))
        except SourceError:
            texts[key], invalid = "", True
    # A supplied barcode must be verifiable; a malformed one cannot disappear
    # while a shared SKU is promoted into a stock operation.
    if texts["ean"]:
        pieces = [part for part in re.split(r"[/,;\s]+", texts["ean"]) if part]
        verified = set(verified_barcodes((texts["ean"],)))
        if not pieces or any(part not in verified for part in pieces):
            invalid = True
    native = False
    for key in ("product_id", "option_set_id"):
        value = raw.get(key)
        if value is None or (not isinstance(value, bool) and value in ("", 0, "0")):
            continue
        native = True
        try:
            _positive_id(value)
        except SourceError:
            invalid = True
    length = raw.get("length")
    if length is not None and isinstance(length, (Decimal, int, float)) and not isinstance(length, bool):
        length = str(length)
    return {"line_key": _uuid(raw.get("uuid")), **texts,
            "title": _optional_text(raw.get("title"), 300), "quantity": _quantity(raw.get("quantity")),
            "unit": _optional_text(raw.get("unit"), 30), "kind": _optional_text(raw.get("type"), 30),
            "parent_uuid": _uuid(raw["parent_uuid"]) if raw.get("parent_uuid") not in (None, "") else "",
            "length": _optional_text(length), "length_unit": _optional_text(raw.get("length_unit"), 30),
            "has_native_identity": native, "identity_invalid": invalid}


def _order(payload: dict, order_number: str) -> tuple[dict, str]:
    rows = payload.get("orders") if isinstance(payload, dict) else None
    if rows == []:
        raise SourceError("order_stock_source_missing", 409)
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise _invalid()
    for field in ("current_page", "number_of_pages", "number_of_items"):
        if type(payload.get(field)) is not int or payload[field] != 1:
            raise _invalid()
    raw = rows[0]
    if _text(raw.get("order_number"), blank=False) != order_number or not isinstance(raw.get("resolved_yn"), bool):
        raise _invalid()
    for field in ("deleted_yn", "deleted"):
        value = raw.get(field)
        if value is not None and not isinstance(value, bool):
            raise _invalid()
        if value is True:
            raise SourceError("order_stock_source_missing", 409)
    products = raw.get("products")
    if not isinstance(products, list) or len(products) > 1000:
        raise _invalid()
    lines = [_line(line) for line in products]
    if len({line["line_key"] for line in lines}) != len(lines):
        raise _invalid()
    created, updated = _timestamp(raw.get("creation_time")), _timestamp(raw.get("last_update_time"))
    if datetime.fromisoformat(updated) < datetime.fromisoformat(created):
        raise _invalid()
    paid_date = _paid_date(raw.get("paid_date"))
    order = {"order_number": order_number, "uuid": _uuid(raw.get("uuid")), "created_at": created,
             "updated_at": updated, "origin": _text(raw.get("origin"), 40, blank=False),
             "status_id": _positive_id(raw.get("status_id")), "paid": paid_date is not None,
             "resolved": raw["resolved_yn"], "lines": lines}
    facts = {**order, "lines": sorted(lines, key=lambda line: line["line_key"]), "paid_date": paid_date}
    return order, _hash(facts)


async def resolve_lines(db, shop: Shop, order_dict: dict) -> list[dict]:
    """Re-resolve only preserved source facts; saved identity fields are ignored."""
    lines = [{key: value for key, value in line.items() if key not in IDENTITY_FIELDS} for line in order_dict["lines"]]
    identities = [RemoteIdentity(shop_id=shop.id, code=line["code"],
                  barcodes=(line["ean"],) if line["ean"] else ()) for line in lines]
    index = await load_identity_index(db, shop.id, identities)
    result = []
    for line, identity in zip(lines, identities):
        reasons, product_id, sku, matched_by = [], None, None, None
        if line["identity_invalid"]:
            classification, reasons = "conflict", ["invalid_identity"]
        elif line["kind"] == "discount":
            classification, reasons = "non_stock", ["non_stock_discount"]
        elif line["kind"] not in ("product", "gift") or (line["parent_uuid"] and line["kind"] != "gift"):
            classification, reasons = "conflict", ["unsupported_line_type"]
        elif not (line["code"] or line["ean"] or line["has_native_identity"]):
            classification, reasons = "manual", ["manual_outside_stock"]
        else:
            resolved = index.resolve(identity)
            classification, product_id, matched_by = resolved.status, resolved.product_id, resolved.matched_by
            reasons = list(resolved.reasons)
            if not line["code"]:
                reasons.append("missing_code_with_identity")
            if product_id is not None:
                sku = index.products[product_id].sku
        if line["quantity"] is None and classification not in ("manual", "non_stock"):
            classification = "conflict"
            reasons.append("invalid_quantity")
        result.append({**line, "classification": classification, "product_id": product_id, "sku": sku,
                       "matched_by": matched_by, "reasons": sorted(set(reasons))})
    return result


def _fetch(shop_code: str, order_number: str | None = None):
    client = None
    try:
        client = UpgatesClient.from_shop(shop_code)
        if order_number is None:
            return client.read_order_statuses()
        # The documented default excludes deleted orders. A filtered list has
        # the complete detail schema and establishes this active-only scope.
        return client.read_order_audit({"order_numbers": order_number, "page": 1})
    except UpgatesError as error:
        if error.status_code in (401, 403):
            raise SourceError("order_stock_upgates_access") from None
        if error.status_code == 429:
            raise SourceError("order_stock_rate_limited", 429) from None
        if error.status_code == 404 and order_number is not None:
            raise SourceError("order_stock_source_missing", 409) from None
        raise SourceError("order_stock_source_unavailable") from None
    finally:
        if client is not None:
            client.session.close()


async def load_statuses(shop_code: str) -> dict:
    return _statuses(await asyncio.to_thread(_fetch, shop_code))


async def load_source(db, shop: Shop, order_number: str) -> dict:
    try:
        normalized = _text(order_number, blank=False)
    except SourceError:
        raise SourceError("order_stock_invalid_order_number", 422) from None
    if normalized != order_number or ";" in normalized:
        raise SourceError("order_stock_invalid_order_number", 422)
    payload, status_payload = await asyncio.to_thread(_fetch, shop.code, order_number)
    statuses = _statuses(status_payload)
    order, source_hash = _order(payload, order_number)
    order["lines"] = await resolve_lines(db, shop, order)
    return {"order": order, "source_hash": source_hash, **statuses}
