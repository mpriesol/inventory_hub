"""Minimal FIFO-cost Upgates transport; durable intent/recovery belong to caller.

All costs received here are EUR per unit excluding VAT. The order's own VAT
basis is independent of the shop's current product price basis. One PUT is
attempted, never retried; an acknowledgement still requires a fresh readback.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re

from inventory_hub.services import order_stock_source as orders
from inventory_hub.services import product_publication_source as products
from inventory_hub.services import stock_publication_source as source


SourceError = source.SourceError
PRECISION = Decimal(".0001")
PRODUCT_STOCK_FIELDS = {
    "stock", "stock_increment", "stock_position", "stocks", "variants_stock",
    "availability", "availability_id", "availability_type", "variants_availability",
    "variants_availability_id", "variants_availability_type", "can_add_to_basket_yn", "limit_orders",
}


def _invalid():
    return SourceError("fifo_cost_invalid_intent", 422)


def _number(value, *, nullable=False):
    if nullable and value is None:
        return None
    try:
        if isinstance(value, bool) or not isinstance(value, (Decimal, int, float, str)):
            raise ValueError
        if isinstance(value, str) and (len(value) > 60 or re.fullmatch(r"\d+(?:\.\d+)?", value) is None):
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite() or not 0 <= result <= Decimal("9999999999.9999"):
            raise ValueError
        return format(result.normalize(), "f")
    except (InvalidOperation, ValueError):
        raise SourceError("fifo_cost_price_unknown", 422) from None


def _vat(value):
    value = _number(value)
    if Decimal(value) > 100:
        raise SourceError("fifo_cost_vat_unknown", 422)
    return value


def _converted(net, with_vat, vat):
    if type(with_vat) is not bool:
        raise SourceError("fifo_cost_price_basis_unknown", 422)
    result = Decimal(_number(net))
    tax = Decimal(_vat(vat))
    if with_vat:
        result *= 1 + tax / 100
    return _number(result.quantize(PRECISION, rounding=ROUND_HALF_UP))


def _canonical(value):
    if isinstance(value, (Decimal, float)):
        return format(Decimal(str(value)).normalize(), "f")
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def _hash(value):
    return hashlib.sha256(json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()


def _product_observation(remote):
    identity = source._target(remote["identity"], frozen=True)
    options = remote["options"]
    if (not isinstance(options, dict) or options.get("currency") != "EUR"
            or options.get("language") != "sk" or type(options.get("prices_with_vat")) is not bool):
        raise SourceError("fifo_cost_price_basis_unknown", 422)
    leaf = deepcopy(remote["leaf"])
    price_rows = leaf.get("prices")
    if not isinstance(price_rows, list) or any(not isinstance(p, dict) for p in price_rows):
        raise SourceError("fifo_cost_price_basis_unknown", 422)
    matches = [p for p in price_rows if p.get("language") == "sk"]
    if len(matches) != 1:
        raise SourceError("fifo_cost_price_basis_unknown", 422)
    price = matches[0]
    vat = _vat(price.get("vat"))
    values = {"price_purchase": _number(price.pop("price_purchase", None), nullable=True)}
    # Independent regular stock publication may change these fields while a
    # purchase-only PUT is in flight. They are absent from our payload and do
    # not determine its price basis. Identity, VAT and all other prices remain
    # guarded; order quantities/content keep their separate strict guard below.
    for field in PRODUCT_STOCK_FIELDS | {"last_update_time"}:
        leaf.pop(field, None)
    return {"kind": "product", "identity": identity, "options": deepcopy(options), "vat": vat,
            "guard": _hash({"identity": identity, "options": options, "leaf": leaf}), "values": values}


def _price_options(client):
    # Purchase price belongs to a language, not to a pricelist. Read only the
    # settings that determine its meaning, fresh on every observation.
    payload = source._request(client, "get", "config")
    if not isinstance(payload, dict) or not source._messages_ok(payload.get("messages")):
        raise SourceError("fifo_cost_price_basis_unknown", 422)
    config = payload.get("config")
    if not isinstance(config, dict) or type(config.get("prices_with_vat_yn")) is not bool:
        raise SourceError("fifo_cost_price_basis_unknown", 422)
    payload = source._request(client, "get", "languages")
    if not isinstance(payload, dict) or not source._messages_ok(payload.get("messages")):
        raise SourceError("fifo_cost_price_basis_unknown", 422)
    languages = payload.get("languages")
    if not isinstance(languages, list) or len(languages) > 1000:
        raise SourceError("fifo_cost_price_basis_unknown", 422)
    selected = [row for row in languages if isinstance(row, dict)
                and row.get("language_id") == "sk" and row.get("active_yn") is True]
    if len(selected) != 1 or selected[0].get("currency_id") != "EUR":
        raise SourceError("fifo_cost_price_basis_unknown", 422)
    return {"prices_with_vat": config["prices_with_vat_yn"], "language": "sk", "currency": "EUR"}


def _read_product(shop_code, target, fingerprint):
    client = source._client(shop_code, fingerprint)
    try:
        remote = products._read(client, target)
        remote["options"] = _price_options(client)
        return _product_observation(remote)
    finally:
        client.session.close()


async def read_product(shop_code, target, fingerprint):
    target = source._target(target)
    return await asyncio.to_thread(_read_product, shop_code, target, fingerprint)


def _order_identity(value):
    try:
        if not isinstance(value, dict) or set(value) != {"order_number", "order_id", "uuid"}:
            raise _invalid()
        number = source._code(value["order_number"])
        if not source._identifier(value["order_id"]):
            raise _invalid()
        identifier = orders._uuid(value["uuid"])
        return {"order_number": number, "order_id": value["order_id"], "uuid": identifier}
    except orders.SourceError:
        raise _invalid() from None


def _order_observation(payload, number):
    raw = deepcopy(source._one(payload, "orders"))
    try:
        normalized, _ = orders._order(payload, number)
    except orders.SourceError as error:
        raise SourceError("fifo_cost_order_source_changed", error.status) from None
    identity = _order_identity({"order_number": normalized["order_number"],
                                "order_id": raw.get("order_id"), "uuid": normalized["uuid"]})
    if raw.get("currency_id") != "EUR":
        raise SourceError("fifo_cost_currency_unsupported", 422)
    if type(raw.get("prices_with_vat_yn")) is not bool:
        raise SourceError("fifo_cost_price_basis_unknown", 422)
    values, lines = {}, []
    for line, original in zip(normalized["lines"], raw["products"]):
        value = _number(original.pop("buy_price", None), nullable=True)
        values[line["line_key"]] = value
        lines.append({"line_key": line["line_key"], "code": line["code"], "quantity": line["quantity"],
                      "kind": line["kind"], "parent_uuid": line["parent_uuid"],
                      "identity_invalid": line["identity_invalid"], "vat": _number(original.get("vat"), nullable=True),
                      "buy_price": value})
    state = {"status_id": normalized["status_id"], "resolved_yn": normalized["resolved"],
             "currency_id": "EUR", "prices_with_vat_yn": raw["prices_with_vat_yn"],
             "paid_date": raw.get("paid_date"), "delivered_date": raw.get("delivered_date")}
    raw.pop("last_update_time", None)
    # Preserve unrelated order details by digest, never persist customer PII.
    raw["products"].sort(key=lambda item: item["uuid"])
    source_lines = sorted(({key: line[key] for key in ("line_key", "code", "ean", "quantity", "kind", "parent_uuid")}
                           for line in normalized["lines"]), key=lambda line: line["line_key"])
    return {"kind": "order", "identity": identity, "state": state, "lines": lines, "source_lines": source_lines,
            "guard": _hash(raw), "values": values}


def _read_order(shop_code, number, fingerprint):
    client = source._client(shop_code, fingerprint)
    try:
        payload = source._request(client, "get", "orders", params={"order_numbers": number, "page": 1})
        return _order_observation(payload, number)
    finally:
        client.session.close()


async def read_order(shop_code, order_number, fingerprint):
    number = source._code(order_number)
    return await asyncio.to_thread(_read_order, shop_code, number, fingerprint)


def _snapshot(remote):
    return {key: deepcopy(remote[key]) for key in ("kind", "identity", "guard", "values")}


def prepare_product(remote, unit_cost_net):
    if remote.get("kind") != "product":
        raise _invalid()
    identity = source._target(remote["identity"], frozen=True)
    cost = _converted(unit_cost_net, remote["options"]["prices_with_vat"], remote["vat"])
    before = _snapshot(remote)
    after = deepcopy(before)
    after["values"]["price_purchase"] = cost
    intent = {"kind": "product", "identity": identity, "before": before, "after": after,
              "language": remote["options"]["language"], "prices_with_vat_yn": remote["options"]["prices_with_vat"]}
    intent["payload"] = _payload(intent)
    return intent


def prepare_order(remote, issued_lines):
    if (remote.get("kind") != "order" or not isinstance(issued_lines, list)
            or not 1 <= len(issued_lines) <= 1000):
        raise _invalid()
    identity = _order_identity(remote["identity"])
    before = _snapshot(remote)
    after = deepcopy(before)
    selected, seen = [], set()
    for issued in issued_lines:
        if not isinstance(issued, dict):
            raise _invalid()
        key, code = issued.get("line_key"), source._code(issued.get("code"))
        if not isinstance(key, str) or key in seen:
            raise _invalid()
        seen.add(key)
        matching = [line for line in remote["lines"] if line["code"].casefold() == code.casefold()]
        if len(matching) > 1:
            raise SourceError("fifo_cost_duplicate_order_code", 409)
        if (len(matching) != 1 or matching[0]["line_key"] != key or matching[0]["code"] != code
                or matching[0]["identity_invalid"] or matching[0]["kind"] not in ("product", "gift")
                or (matching[0]["parent_uuid"] and matching[0]["kind"] != "gift")
                or matching[0]["quantity"] is None or orders._quantity(issued.get("quantity")) != matching[0]["quantity"]):
            raise SourceError("fifo_cost_order_lines_changed", 409)
        after["values"][key] = _converted(issued.get("unit_cost_net"),
            remote["state"]["prices_with_vat_yn"], matching[0]["vat"])
        selected.append({"line_key": key, "code": code})
    intent = {"kind": "order", "identity": identity, "before": before, "after": after,
              "selected": selected, "prices_with_vat_yn": remote["state"]["prices_with_vat_yn"]}
    intent["payload"] = _payload(intent)
    return intent


def matches_before(intent, remote):
    return intent["before"] == _snapshot(remote)


def matches_after(intent, remote):
    return intent["after"] == _snapshot(remote)


def _payload(intent):
    """Reconstruct an allowlisted body; never pass a stored arbitrary PUT through."""
    kind, identity = intent["kind"], intent["identity"]
    for state in (intent["before"], intent["after"]):
        if (set(state) != {"kind", "identity", "guard", "values"} or state["kind"] != kind
                or state["identity"] != identity or not isinstance(state["guard"], str)
                or re.fullmatch(r"[0-9a-f]{64}", state["guard"]) is None):
            raise _invalid()
    if intent["before"]["guard"] != intent["after"]["guard"]:
        raise _invalid()
    if kind == "product":
        identity = source._target(identity, frozen=True)
        if intent.get("language") != "sk" or any(set(intent[k]["values"]) != {"price_purchase"} for k in ("before", "after")):
            raise _invalid()
        value = _number(intent["after"]["values"]["price_purchase"])
        leaf = {"code": identity["code"], "prices": [{"language": "sk", "price_purchase": float(value)}]}
        parent = leaf if identity["variant_code"] is None else {"code": identity["parent_code"], "variants": [leaf]}
        return {"products": [parent]}
    if kind != "order" or type(intent.get("prices_with_vat_yn")) is not bool:
        raise _invalid()
    identity = _order_identity(identity)
    selected = intent.get("selected")
    if not isinstance(selected, list) or not 1 <= len(selected) <= 1000:
        raise _invalid()
    rows, keys, codes = [], set(), set()
    for line in selected:
        if not isinstance(line, dict) or set(line) != {"line_key", "code"}:
            raise _invalid()
        try:
            key = orders._uuid(line["line_key"])
        except orders.SourceError:
            raise _invalid() from None
        code = source._code(line["code"])
        if key in keys or code.casefold() in codes or key not in intent["before"]["values"]:
            raise _invalid()
        keys.add(key); codes.add(code.casefold())
        rows.append({"code": code, "buy_price": float(_number(intent["after"]["values"][key]))})
    if (set(intent["before"]["values"]) != set(intent["after"]["values"])
            or any(intent["before"]["values"][key] != value
                   for key, value in intent["after"]["values"].items() if key not in keys)):
        raise _invalid()
    return {"orders": [{"order_number": identity["order_number"],
                        "prices_with_vat_yn": intent["prices_with_vat_yn"], "products": rows}],
            "send_emails_yn": False, "send_sms_yn": False, "delete_missing_products_yn": False}


def _order_acknowledged(payload, identity):
    if not isinstance(payload, dict) or not source._messages_ok(payload.get("messages")):
        return False
    # The official response schema declares a single object; deployments also
    # use the usual singleton array. Neither shape permits another order.
    rows = payload.get("orders")
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return False
    row = rows[0]
    return (row.get("order_number") == identity["order_number"] and row.get("updated_yn") is True
            and source._messages_ok(row.get("messages"))
            and ("order_id" not in row or row["order_id"] == identity["order_id"])
            and ("uuid" not in row or row["uuid"] == identity["uuid"]))


def _write(shop_code, intent, fingerprint, payload):
    client = source._client(shop_code, fingerprint)
    try:
        result = source._request(client, "put", "products" if intent["kind"] == "product" else "orders", payload=payload)
        acknowledged = (source._acknowledged(result, intent["identity"]) if intent["kind"] == "product"
                        else _order_acknowledged(result, intent["identity"]))
        if not acknowledged:
            raise SourceError("fifo_cost_write_unconfirmed", uncertain=True)
        return {"acknowledged": True}
    finally:
        client.session.close()


async def write_once(shop_code, intent, fingerprint):
    try:
        payload = _payload(intent)
        if payload != intent.get("payload"):
            raise _invalid()
    except (KeyError, TypeError, AttributeError):
        raise _invalid() from None
    return await asyncio.to_thread(_write, shop_code, intent, fingerprint, payload)
