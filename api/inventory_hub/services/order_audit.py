"""Read-only order readiness: normalize a bounded page, never import history.

The projection intentionally has no customer, address, note, attachment, price
or raw-payload field. Matching is shared with product import; order rows without
a stocked identity stay manual, whereas coded unresolved rows need review.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import unicodedata

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.db_models import Shop
from inventory_hub.services.product_identity import RemoteIdentity, load_identity_index
from inventory_hub.services.upgates import UpgatesClient, UpgatesError


class OrderAuditError(Exception):
    def __init__(self, code: str, status: int = 502):
        self.code, self.status = code, status
        super().__init__(code)


def _text(value, limit: int = 100) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(c for c in value.strip() if c >= " " and c != "\x7f")[:limit]


def _positive_id(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 18 and int(value) > 0:
        return int(value)
    return None


def _identity_text(value) -> str:
    """Identity values must never match by a truncated or repaired prefix."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if len(value) <= 100 and _text(value) == value else ""


def _date(value) -> str | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.isoformat()


def _quantity(value) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        quantity = Decimal(str(value))
        if (not quantity.is_finite() or quantity <= 0 or quantity > Decimal("999999999.999")
                or quantity != quantity.quantize(Decimal("0.001"))):
            return None
        return format(quantity, "f")
    except (InvalidOperation, ValueError):
        return None


def _label(value: str) -> str:
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", value.casefold())
                             if not unicodedata.combining(c)).split())


def _status_map(payload: dict) -> dict[int, dict]:
    rows = payload.get("order_statuses")
    if not isinstance(rows, list) or len(rows) > 1000:
        raise OrderAuditError("order_audit_invalid_response")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise OrderAuditError("order_audit_invalid_response")
        identifier = _positive_id(row.get("id"))
        if identifier is None or identifier in result:
            raise OrderAuditError("order_audit_invalid_response")
        names = [d for d in row.get("descriptions", []) if isinstance(d, dict)] if isinstance(row.get("descriptions"), list) else []
        if any(not isinstance(d.get("language_id"), str) for d in names):
            raise OrderAuditError("order_audit_invalid_response")
        names.sort(key=lambda d: {"sk": 0, "cs": 1}.get(d["language_id"], 2))
        result[identifier] = {
            "type": _text(row.get("type"), 40),
            "name": next((_text(d.get("name")) for d in names if _text(d.get("name"))), ""),
            "labels": {_label(_text(d.get("name"))) for d in names},
        }
    return result


def _candidate(raw: dict, status: dict | None) -> tuple[str, str]:
    # These are review signals, not a persisted physical lock or proof of an
    # unprocessed sale. Return/claim/history cannot be inferred from a snapshot.
    if status is None:
        return "review", "unknown_status"
    kind = status["type"]
    if kind == "Canceled":
        return "cancel", "cancelled_status"
    if kind in ("PaymentFailed", "PaymentCanceled"):
        return "review", "requires_review"
    if status["labels"].intersection({"reklamacia", "reklamovana", "vratena", "vratenie", "neprevzata",
                                       "nedorucena", "returned", "return", "claim", "undelivered"}):
        return "review", "requires_review"
    if kind == "Sent" or status["labels"].intersection({"odoslana", "vyzdvihnuta"}):
        return "issue", "confirmed_issue_status"
    if (kind == "Received" and raw.get("origin") == "cash-register" and _date(raw.get("paid_date"))
            and raw.get("resolved_yn") is True):
        return "issue", "cash_register_completed"
    if kind == "Received":
        return "reserve", "received_status"
    return "review", "requires_review"


def _line_identity(shop_id: int, raw: dict) -> RemoteIdentity:
    # Upgates documents product_id and option_set_id as orientational, and
    # code as the matching key. A native product ID is not a variant ID.
    return RemoteIdentity(
        shop_id=shop_id, code=_identity_text(raw.get("code")),
        is_variant=True if _positive_id(raw.get("option_set_id")) else None,
        barcodes=(_identity_text(raw.get("ean")),) if _identity_text(raw.get("ean")) else (),
    )


def _normalize_line(raw: dict, position: int, identity: RemoteIdentity, index) -> dict:
    code, ean = _text(raw.get("code")), _text(raw.get("ean"))
    quantity = _quantity(raw.get("quantity"))
    kind = _text(raw.get("type"), 30)
    reasons = []
    manual = not (code or ean or _positive_id(raw.get("product_id")) or _positive_id(raw.get("option_set_id")))
    product_id = sku = matched_by = None
    invalid_identity = (
        any(raw.get(key) not in (None, "") and not _identity_text(raw.get(key)) for key in ("code", "ean"))
        or any(raw.get(key) not in (None, "", 0, "0") and _positive_id(raw.get(key)) is None
               for key in ("product_id", "option_set_id"))
    )
    if kind == "discount":
        classification = "non_stock"
        reasons.append("non_stock_discount")
    elif invalid_identity:
        classification = "conflict"
        reasons.append("invalid_identity")
    elif kind not in ("", "product", "gift") or (kind != "gift" and _text(raw.get("parent_uuid"))):
        classification = "conflict"
        reasons.append("unsupported_line_type")
    elif manual:
        classification = "manual"
        reasons.append("manual_outside_stock")
    else:
        resolution = index.resolve(identity)
        classification = resolution.status
        product_id, matched_by = resolution.product_id, resolution.matched_by
        reasons.extend(resolution.reasons)
        if not code:
            reasons.append("missing_code_with_identity")
            # A barcode can suggest a product, but this audit never creates
            # a remote identity or promotes a code-less line to stock-ready.
            if classification == "mapped":
                classification = "identified"
        if product_id is not None and product_id in index.products:
            sku = index.products[product_id].sku
    if classification not in ("manual", "non_stock") and _text(raw.get("unit"), 30) and _label(_text(raw.get("unit"), 30)) != "ks":
        reasons.append("unit_requires_review")
    if quantity is None and classification != "non_stock":
        classification = "conflict"
        reasons.append("invalid_quantity")
    line_uuid = _text(raw.get("uuid"))
    if not line_uuid:
        reasons.append("missing_line_uuid")
    return {
        "line_key": line_uuid or f"row:{position}", "code": code,
        "title": _text(raw.get("title"), 300), "ean": ean, "quantity": quantity,
        "unit": _text(raw.get("unit"), 30), "classification": classification,
        "product_id": product_id, "sku": sku, "matched_by": matched_by,
        "reasons": sorted(set(reasons)),
    }


async def normalize_order_page(db: AsyncSession, shop: Shop, orders_payload: dict, status_payload: dict,
                               *, page: int, fetched_at: datetime) -> dict:
    """Normalize only the safe, reusable order/line facts of one API page."""
    statuses = _status_map(status_payload)
    raw_orders = orders_payload.get("orders")
    if not isinstance(raw_orders, list) or len(raw_orders) > 100:
        raise OrderAuditError("order_audit_invalid_response")
    identities, raw_lines, order_numbers = [], [], set()
    for order in raw_orders:
        if not isinstance(order, dict) or not _identity_text(order.get("order_number")):
            raise OrderAuditError("order_audit_invalid_response")
        order_number = _identity_text(order["order_number"])
        if order_number in order_numbers:
            raise OrderAuditError("order_audit_invalid_response")
        order_numbers.add(order_number)
        lines = order.get("products")
        if not isinstance(lines, list) or len(lines) > 1000 or any(not isinstance(line, dict) for line in lines):
            raise OrderAuditError("order_audit_invalid_response")
        raw_lines.append(lines)
        identities.extend(_line_identity(shop.id, line) for line in lines)
    if len(identities) > 5000:
        raise OrderAuditError("order_audit_response_limit")
    index = await load_identity_index(db, shop.id, identities)
    summary = dict.fromkeys(("orders", "lines", "mapped", "identified", "manual", "non_stock", "unresolved", "conflict"), 0)
    orders = []
    offset = 0
    for raw, lines in zip(raw_orders, raw_lines):
        status_id = _positive_id(raw.get("status_id"))
        status = statuses.get(status_id)
        candidate, reason = _candidate(raw, status)
        normalized = [_normalize_line(line, position, identities[offset + position], index)
                      for position, line in enumerate(lines)]
        offset += len(lines)
        seen, warnings = set(), []
        for line in normalized:
            if line["line_key"] in seen:
                line["classification"] = "conflict"
                line["reasons"].append("duplicate_line_uuid")
                warnings.append("duplicate_line_uuid")
            seen.add(line["line_key"])
            summary[line["classification"]] += 1
        if not normalized:
            warnings.append("no_product_lines")
        if any(line["classification"] in ("identified", "unresolved", "conflict") for line in normalized):
            warnings.append("identity_review_required")
        if any("missing_line_uuid" in line["reasons"] for line in normalized):
            warnings.append("missing_line_uuid")
        if any("unit_requires_review" in line["reasons"] for line in normalized):
            warnings.append("unit_requires_review")
        orders.append({
            "order_number": _text(raw.get("order_number")), "origin": _text(raw.get("origin"), 40),
            "created_at": _date(raw.get("creation_time")), "updated_at": _date(raw.get("last_update_time")),
            "status_id": status_id, "status_name": _text(raw.get("status")) or (status["name"] if status else ""),
            "status_type": status["type"] if status else None, "paid": bool(_date(raw.get("paid_date"))),
            "resolved": raw.get("resolved_yn") is True, "delivered": bool(_date(raw.get("delivered_date"))),
            "candidate": candidate, "candidate_reason": reason, "lines": normalized, "warnings": sorted(set(warnings)),
        })
    summary.update(orders=len(orders), lines=len(identities))
    metadata = {}
    for key in ("current_page", "number_of_pages", "number_of_items"):
        value = orders_payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise OrderAuditError("order_audit_invalid_response")
        metadata[key] = value
    if metadata["current_page"] != page:
        raise OrderAuditError("order_audit_invalid_response")
    return {
        "shop": shop.code, "fetched_at": fetched_at.isoformat(), "read_only": True,
        "page": page, "number_of_pages": metadata["number_of_pages"],
        "number_of_items": metadata["number_of_items"], "has_more": page < metadata["number_of_pages"],
        "summary": summary, "warnings": ["snapshot_not_stock_history"], "orders": orders,
    }


def _fetch_page(shop_code: str, days: int, page: int, at: datetime) -> tuple[dict, dict]:
    client = None
    try:
        client = UpgatesClient.from_shop(shop_code)
        return client.read_order_audit({
            "page": page, "order_by": "creation_time", "order_dir": "desc",
            "creation_time_from": (at - timedelta(days=days)).isoformat(),
        })
    except UpgatesError as error:
        if error.status_code in (401, 403):
            raise OrderAuditError("order_audit_upgates_access", 502) from None
        if error.status_code == 429:
            raise OrderAuditError("order_audit_rate_limited", 429) from None
        raise OrderAuditError("order_audit_upgates_unavailable", 502) from None
    finally:
        if client is not None:
            client.session.close()


async def audit_orders(db: AsyncSession, shop_code: str, days: int, page: int) -> dict:
    shop = await db.scalar(select(Shop).where(Shop.code == shop_code, Shop.is_active.is_(True)))
    if shop is None or shop.platform != "upgates":
        raise OrderAuditError("order_audit_shop_not_found", 404)
    at = datetime.now(timezone.utc)
    payload, statuses = await asyncio.to_thread(_fetch_page, shop.code, days, page, at)
    return await normalize_order_page(db, shop, payload, statuses, page=page, fetched_at=at)
