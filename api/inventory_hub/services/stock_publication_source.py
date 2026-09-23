"""Strict single-leaf stock transport. Durable intent and readback belong to the caller.

The write helper attempts one absolute PUT. It never retries, reads siblings,
updates local stock, or treats an acknowledgement as verified remote stock.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation
import json
import re
from time import monotonic
from urllib.parse import quote

import requests

from inventory_hub.services.upgates import UpgatesClient, UpgatesError, connection_fingerprint, _retry_after_seconds


MAX_RESPONSE_BYTES = 8_000_000
MAX_RESPONSE_SECONDS = 30
MAX_SAFE_INTEGER = 9_007_199_254_740_991
IDENTITY_KEYS = {"code", "parent_code", "variant_code", "product_id", "variant_id"}


class SourceError(Exception):
    def __init__(self, code: str, status: int = 502, retry_after: int | None = None, uncertain: bool = False):
        self.code, self.status = code, status
        self.retry_after, self.uncertain = retry_after, uncertain
        super().__init__(code)


def _invalid_target():
    return SourceError("stock_publication_invalid_target", 422)


def _code(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 100 or value != value.strip()
            or value in (".", "..") or ";" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise _invalid_target()
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise _invalid_target() from None
    return value


def _identifier(value):
    return type(value) is int and 1 <= value <= MAX_SAFE_INTEGER


def _target(value, *, frozen=False):
    if not isinstance(value, dict) or set(value) - IDENTITY_KEYS or not {"code", "parent_code", "variant_code"} <= set(value):
        raise _invalid_target()
    target = {key: _code(value[key]) for key in ("code", "parent_code")}
    variant = value["variant_code"]
    target["variant_code"] = _code(variant) if variant is not None else None
    if (variant is None and target["parent_code"] != target["code"]) or (variant is not None and variant != target["code"]):
        raise _invalid_target()
    if frozen and set(value) != IDENTITY_KEYS:
        raise _invalid_target()
    if "product_id" in value:
        if not _identifier(value["product_id"]):
            raise _invalid_target()
        target["product_id"] = value["product_id"]
    if "variant_id" in value:
        if (variant is None and value["variant_id"] is not None) or (variant is not None and not _identifier(value["variant_id"])):
            raise _invalid_target()
        target["variant_id"] = value["variant_id"]
    return target


def _quantity(value, *, outgoing=False):
    if outgoing and (not isinstance(value, str) or re.fullmatch(r"0|[1-9][0-9]{0,15}", value) is None):
        raise SourceError("stock_publication_invalid_quantity", 422)
    try:
        if value is None or isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
            raise ValueError
        if isinstance(value, str) and (len(value) > 100 or re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", value) is None):
            raise ValueError
        number = Decimal(value)
        if not number.is_finite() or number.copy_abs() > MAX_SAFE_INTEGER or number != number.to_integral_value():
            raise ValueError
        if outgoing and number < 0:
            raise ValueError
        return str(int(number))
    except (ValueError, InvalidOperation, OverflowError):
        code = "stock_publication_invalid_quantity" if outgoing else "stock_publication_stock_unknown"
        raise SourceError(code, 422 if outgoing else 409) from None


def _messages_ok(value):
    return value is None or (isinstance(value, list) and all(
        isinstance(message, dict) and message.get("level") == "info" for message in value))


def _one(payload, key):
    if not isinstance(payload, dict) or not _messages_ok(payload.get("messages")):
        raise SourceError("stock_publication_invalid_response")
    rows = payload.get(key)
    if not isinstance(rows, list):
        raise SourceError("stock_publication_invalid_response")
    metadata = [payload.get(field) for field in ("current_page", "current_page_items", "number_of_items", "number_of_pages")]
    if any(type(value) is not int for value in metadata):
        raise SourceError("stock_publication_invalid_response")
    if not rows and metadata[0] == 1 and metadata[1:3] == [0, 0] and metadata[3] in (0, 1):
        raise SourceError("stock_publication_source_missing", 409)
    if len(rows) != 1 or metadata != [1, 1, 1, 1] or not isinstance(rows[0], dict) or not _messages_ok(rows[0].get("messages")):
        raise SourceError("stock_publication_invalid_response")
    return rows[0]


def _observation(payload, target):
    variant = target["variant_code"] is not None
    row = _one(payload, "variants" if variant else "products")
    if (row.get("code") != target["code"] or not _identifier(row.get("product_id"))
            or (variant and (row.get("product_code") != target["parent_code"] or not _identifier(row.get("variant_id"))))):
        raise SourceError("stock_publication_identity_changed", 409)
    if not variant and (row.get("variants_exists_yn") is not False or row.get("variants") not in (None, [])
                        or row.get("set_yn", False) is not False):
        raise SourceError("stock_publication_identity_changed", 409)
    identity = {"code": target["code"], "parent_code": target["parent_code"], "variant_code": target["variant_code"],
                "product_id": row["product_id"], "variant_id": row["variant_id"] if variant else None}
    if any(target[key] != identity[key] for key in ("product_id", "variant_id") if key in target):
        raise SourceError("stock_publication_identity_changed", 409)
    return {"identity": identity, "quantity": _quantity(row.get("stock"))}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Nonfinite JSON number")


def _request(client, method, path, *, params=None, payload=None):
    writing = method == "put"
    response = None
    started_at = monotonic()

    def check_elapsed():
        # Checked between chunks; requests' socket timeout remains an inactivity
        # timeout, so cancelling the caller still cannot retract a sent PUT.
        if monotonic() - started_at > MAX_RESPONSE_SECONDS:
            raise SourceError("stock_publication_write_unconfirmed" if writing else "stock_publication_source_unavailable",
                              uncertain=writing)

    try:
        options = {"timeout": (5, 25), "verify": True, "allow_redirects": False, "stream": True}
        if writing:
            response = client.session.put(f"{client.base_url}/{path}", json=payload, **options)
        else:
            response = client.session.get(f"{client.base_url}/{path}", params=params or {}, **options)
        status = response.status_code
        if status in (401, 403):
            raise SourceError("stock_publication_upgates_access", 502)
        if status == 429:
            raise SourceError("stock_publication_rate_limited", 429,
                              _retry_after_seconds(response.headers.get("Retry-After")))
        if not writing and status == 404:
            raise SourceError("stock_publication_source_missing", 409)
        if status != 200:
            raise SourceError("stock_publication_write_unconfirmed" if writing else "stock_publication_source_unavailable",
                              uncertain=writing)
        check_elapsed()
        chunks, size = [], 0
        for chunk in response.iter_content(chunk_size=65536):
            check_elapsed()
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise SourceError("stock_publication_write_unconfirmed" if writing else "stock_publication_invalid_response",
                                  uncertain=writing)
            chunks.append(chunk)
        try:
            return json.loads(b"".join(chunks), parse_float=Decimal, parse_constant=_invalid_constant,
                              object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError, RecursionError):
            raise SourceError("stock_publication_write_unconfirmed" if writing else "stock_publication_invalid_response",
                              uncertain=writing) from None
    except requests.RequestException:
        raise SourceError("stock_publication_write_unconfirmed" if writing else "stock_publication_source_unavailable",
                          uncertain=writing) from None
    finally:
        if response is not None:
            response.close()


def _client(shop_code, expected_target_fingerprint):
    if not isinstance(shop_code, str) or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,49}", shop_code) is None:
        raise _invalid_target()
    if not isinstance(expected_target_fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", expected_target_fingerprint) is None:
        raise _invalid_target()
    try:
        client = UpgatesClient.from_shop(shop_code)
    except (UpgatesError, OSError, ValueError, TypeError):
        raise SourceError("stock_publication_connection_missing") from None
    auth = client.session.auth
    login = auth[0] if isinstance(auth, (tuple, list)) and len(auth) == 2 else getattr(auth, "username", None)
    if connection_fingerprint(client.base_url, login) != expected_target_fingerprint:
        client.session.close()
        raise SourceError("stock_publication_target_changed", 409)
    return client


def _read_stock(shop_code, target, expected_target_fingerprint):
    client = _client(shop_code, expected_target_fingerprint)
    try:
        if target["variant_code"] is None:
            payload = _request(client, "get", f'products/{quote(target["code"], safe="")}/simple')
        else:
            payload = _request(client, "get", "products/variants",
                               params={"variant_codes": target["code"], "page": 1, "current_page_items": 100})
        return _observation(payload, target)
    finally:
        client.session.close()


def _acknowledged(payload, identity):
    if not isinstance(payload, dict) or not _messages_ok(payload.get("messages")):
        return False
    products = payload.get("products")
    if not isinstance(products, list) or len(products) != 1 or not isinstance(products[0], dict):
        return False
    parent = products[0]
    if (parent.get("code") != identity["parent_code"] or not _identifier(parent.get("product_id"))
            or parent["product_id"] != identity["product_id"] or parent.get("updated_yn") is not True
            or not _messages_ok(parent.get("messages"))):
        return False
    if identity["variant_code"] is None:
        return parent.get("variants") in (None, [])
    variants = parent.get("variants")
    if not isinstance(variants, list) or len(variants) != 1 or not isinstance(variants[0], dict):
        return False
    leaf = variants[0]
    return (leaf.get("code") == identity["variant_code"] and _identifier(leaf.get("variant_id"))
            and leaf["variant_id"] == identity["variant_id"] and leaf.get("updated_yn") is True
            and _messages_ok(leaf.get("messages")))


def _write_stock_once(shop_code, identity, quantity, expected_target_fingerprint):
    client = _client(shop_code, expected_target_fingerprint)
    try:
        leaf = {"code": identity["code"], "stock": int(quantity)}
        product = leaf if identity["variant_code"] is None else {"code": identity["parent_code"], "variants": [leaf]}
        payload = _request(client, "put", "products", payload={"products": [product]})
        if not _acknowledged(payload, identity):
            raise SourceError("stock_publication_write_unconfirmed", uncertain=True)
        return {"acknowledged": True}
    finally:
        client.session.close()


async def read_stock(shop_code: str, target: dict, expected_target_fingerprint: str) -> dict:
    return await asyncio.to_thread(_read_stock, shop_code, _target(target), expected_target_fingerprint)


async def write_stock_once(shop_code: str, frozen_identity: dict, quantity: str, expected_target_fingerprint: str) -> dict:
    # Cancelling to_thread cannot cancel an already dispatched request. The
    # caller must commit its sending/quarantine state before awaiting this.
    return await asyncio.to_thread(_write_stock_once, shop_code, _target(frozen_identity, frozen=True),
                                   _quantity(quantity, outgoing=True), expected_target_fingerprint)
