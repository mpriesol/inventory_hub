"""Controlled order stock processing: fresh source, frozen review, atomic local effect."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import unicodedata

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.db_models import Shop, Warehouse
from inventory_hub.db_models_ext import ShopOrder
from inventory_hub.order_stock_models import OrderStockPolicy, OrderStockPreview
from inventory_hub.order_stock_types import OrderStockApplyRequest, OrderStockConfigureRequest, OrderStockPreviewRequest
from inventory_hub.services import order_stock_ledger as ledger
from inventory_hub.services.order_stock_source import load_source, load_statuses, resolve_lines
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK


EXPIRY_MINUTES = 30
RETURN_LABELS = {"reklamacia", "reklamovana", "vratena", "vratenie", "neprevzata", "nedorucena",
                 "returned", "return", "claim", "undelivered"}


class OrderStockError(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


def now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _label(value: str) -> str:
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", value.casefold())
                             if not unicodedata.combining(c)).split())


def _suggested(status: dict) -> str:
    label = _label(status["name"])
    if label in RETURN_LABELS or status["type"] in ("PaymentFailed", "PaymentCanceled"):
        return "review"
    if status["type"] == "Canceled":
        return "cancel"
    if status["type"] == "Sent" or label in ("odoslana", "vyzdvihnuta"):
        return "issue"
    if status["type"] == "Received":
        return "reserve"
    return "review"


def _allowed(status: dict) -> list[str]:
    suggestion = _suggested(status)
    if suggestion != "review":
        return ["review", suggestion]
    if status["type"] == "Custom" and _label(status["name"]) not in RETURN_LABELS:
        return ["review", "reserve"]
    return ["review"]


async def _shop(db: AsyncSession, code: str, *, lock=False) -> Shop:
    query = select(Shop).where(Shop.code == code, Shop.is_active.is_(True), Shop.platform == "upgates")
    if lock:
        query = query.with_for_update(read=True).execution_options(populate_existing=True)
    shop = await db.scalar(query)
    if shop is None:
        raise OrderStockError("order_stock_shop_not_found", 404)
    return shop


async def _policy(db: AsyncSession, shop_id: int, *, lock=False) -> OrderStockPolicy:
    query = select(OrderStockPolicy).where(OrderStockPolicy.shop_id == shop_id)
    if lock:
        query = query.with_for_update(read=True).execution_options(populate_existing=True)
    policy = await db.scalar(query)
    if policy is None:
        raise OrderStockError("order_stock_not_configured")
    return policy


async def _warehouse(db: AsyncSession, warehouse_id: int, *, lock=False) -> Warehouse:
    query = select(Warehouse).where(Warehouse.id == warehouse_id, Warehouse.is_active.is_(True))
    if lock:
        query = query.with_for_update(read=True).execution_options(populate_existing=True)
    warehouse = await db.scalar(query)
    if warehouse is None:
        raise OrderStockError("order_stock_warehouse_unavailable")
    return warehouse


async def _options(db: AsyncSession, shop: Shop, statuses: dict) -> dict:
    warehouses = (await db.scalars(select(Warehouse).where(Warehouse.is_active.is_(True))
                                  .order_by(Warehouse.code))).all()
    policy = await db.get(OrderStockPolicy, shop.id)
    selected = await db.get(Warehouse, policy.warehouse_id) if policy else None
    return {"shop": {"id": shop.id, "code": shop.code, "name": shop.name},
            "warehouses": [{"id": row.id, "code": row.code, "name": row.name} for row in warehouses],
            **statuses, "suggested_actions": {str(row["id"]): _suggested(row) for row in statuses["statuses"]},
            "allowed_actions": {str(row["id"]): _allowed(row) for row in statuses["statuses"]},
            "policy": {"warehouse_id": policy.warehouse_id, "warehouse_code": selected.code,
                       "starts_at": policy.starts_at.isoformat(), "revision": policy.revision,
                       "status_actions": policy.status_actions} if policy else None}


async def options(db: AsyncSession, shop_code: str) -> dict:
    shop = await _shop(db, shop_code)
    statuses = await load_statuses(shop.code)
    return await _options(db, shop, statuses)


async def configure(db: AsyncSession, payload: OrderStockConfigureRequest) -> dict:
    shop = await _shop(db, payload.shop_code)
    statuses = await load_statuses(shop.code)
    if statuses["status_hash"] != payload.status_hash:
        raise OrderStockError("order_stock_statuses_changed")
    if set(payload.status_actions) != {str(row["id"]) for row in statuses["statuses"]}:
        raise OrderStockError("order_stock_invalid_status_actions", 422)
    for status in statuses["statuses"]:
        action = payload.status_actions[str(status["id"])]
        if (_label(status["name"]) in RETURN_LABELS or status["type"] in ("PaymentFailed", "PaymentCanceled")) and action != "review":
            raise OrderStockError("order_stock_return_requires_review", 422)
        if status["type"] == "Canceled" and action not in ("cancel", "review"):
            raise OrderStockError("order_stock_invalid_status_actions", 422)
        if action not in _allowed(status):
            raise OrderStockError("order_stock_invalid_status_actions", 422)
    # Serialize first activation and later mapping edits on an existing shop row.
    shop = await db.scalar(select(Shop).where(Shop.id == shop.id).with_for_update()
                           .execution_options(populate_existing=True))
    if not shop.is_active or shop.platform != "upgates":
        raise OrderStockError("order_stock_shop_not_found", 404)
    warehouse = await db.scalar(select(Warehouse).where(Warehouse.code == payload.warehouse_code,
                                                        Warehouse.is_active.is_(True)).with_for_update(read=True))
    if warehouse is None:
        raise OrderStockError("order_stock_warehouse_unavailable")
    policy = await db.scalar(select(OrderStockPolicy).where(OrderStockPolicy.shop_id == shop.id)
                             .with_for_update().execution_options(populate_existing=True))
    if policy is None:
        policy = OrderStockPolicy(shop_id=shop.id, warehouse_id=warehouse.id, starts_at=now(), revision=1,
                                  status_actions=payload.status_actions, status_hash=payload.status_hash,
                                  statuses=statuses["statuses"])
        db.add(policy)
    else:
        if warehouse.id != policy.warehouse_id:
            raise OrderStockError("order_stock_warehouse_fixed")
        if policy.status_actions != payload.status_actions or policy.status_hash != payload.status_hash:
            policy.status_actions, policy.status_hash = payload.status_actions, payload.status_hash
            policy.statuses = statuses["statuses"]
            policy.revision += 1
    await db.flush()
    result = await _options(db, shop, statuses)
    await db.commit()
    return result


def _action(policy: OrderStockPolicy, source: dict, status_hash: str, statuses: list) -> str:
    if status_hash != policy.status_hash:
        raise OrderStockError("order_stock_statuses_changed")
    created = datetime.fromisoformat(source["created_at"])
    if created < policy.starts_at:
        raise OrderStockError("order_stock_before_cutover")
    if created > now():
        raise OrderStockError("order_stock_source_date_invalid")
    status = next((row for row in statuses if row["id"] == source["status_id"]), None)
    if status is None:
        raise OrderStockError("order_stock_unknown_status")
    if _label(status["name"]) in RETURN_LABELS:
        raise OrderStockError("order_stock_return_requires_review")
    action = policy.status_actions.get(str(source["status_id"]), "review")
    if action == "review":
        raise OrderStockError("order_stock_status_requires_review")
    # The configured received/reservation state still requires actual POS
    # payment and completion; a paid web order alone remains reserved.
    if (action == "reserve" and status["type"] == "Received" and source["origin"] == "cash-register"
            and source["paid"] and source["resolved"]):
        return "issue"
    return action


def _content(source: dict) -> dict:
    """Physical lock covers all line identities/quantities, not later timestamps."""
    ignored = {"title", "classification", "sku", "matched_by", "reasons"}
    return {"uuid": source["uuid"], "order_number": source["order_number"],
            "lines": sorted(({key: value for key, value in line.items() if key not in ignored}
                              for line in source["lines"]), key=lambda row: row["line_key"])}


def _public(row: OrderStockPreview) -> dict:
    return {**row.preview_data, "preview_hash": row.preview_hash, "status": row.status,
            "completed_at": row.completed_at.isoformat() if row.completed_at else None, "result": row.result}


def _response(row: OrderStockPreview) -> dict:
    return {"ready": row.preview_data["plan"]["ready"], "errors": row.preview_data["plan"]["errors"],
            "preview": _public(row)}


async def get_preview(db: AsyncSession, preview_id: str) -> dict:
    row = await db.get(OrderStockPreview, str(preview_id))
    if row is None:
        raise OrderStockError("order_stock_preview_not_found", 404)
    return _public(row)


async def list_previews(db: AsyncSession, shop_code: str) -> dict:
    shop = await _shop(db, shop_code)
    rows = (await db.scalars(select(OrderStockPreview).where(OrderStockPreview.shop_id == shop.id)
                            .order_by(OrderStockPreview.created_at.desc(), OrderStockPreview.id).limit(20))).all()
    return {"previews": [{"id": row.id, "shop_code": shop.code,
                           "order_number": row.preview_data["source"]["order_number"],
                           "action": row.preview_data["action"], "status": row.status,
                           "created_at": row.created_at.isoformat(), "expires_at": row.expires_at.isoformat()}
                          for row in rows]}


async def preview(db: AsyncSession, payload: OrderStockPreviewRequest) -> dict:
    request_hash = _hash(payload.model_dump(mode="json"))
    preview_id = str(payload.request_id)
    existing = await db.get(OrderStockPreview, preview_id)
    if existing is not None:
        if existing.request_hash != request_hash:
            raise OrderStockError("order_stock_request_changed")
        return _response(existing)
    shop = await _shop(db, payload.shop_code)
    await _policy(db, shop.id)
    fetched = await load_source(db, shop, payload.order_number)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": IDENTITY_WRITE_LOCK})
    shop = await _shop(db, shop.code, lock=True)
    policy = await _policy(db, shop.id, lock=True)
    warehouse = await _warehouse(db, policy.warehouse_id, lock=True)
    source = fetched["order"]
    source["lines"] = await resolve_lines(db, shop, source)
    action = _action(policy, source, fetched["status_hash"], fetched["statuses"])
    try:
        order = await ledger.get_or_create_order(db, shop, source, warehouse.id)
        result = None
        if order.stock_state == "issued":
            if action != "issue" or _content(source) != _content(order.stock_snapshot):
                raise OrderStockError("order_stock_issued_locked")
            action, result = "noop", order.stock_issue_result
            plan = {"ready": True, "errors": [], "lines": [], "effects": [], "excluded_lines": []}
        else:
            plan = await ledger.plan_order(db, order, source, action, warehouse.id, lock=True)
    except ledger.LedgerError as error:
        raise OrderStockError(error.code, error.status) from None
    at = now()
    expires = at + timedelta(minutes=EXPIRY_MINUTES)
    snapshot = {"id": preview_id, "created_at": at.isoformat(), "expires_at": expires.isoformat(),
                "shop_code": shop.code, "warehouse": {"id": warehouse.id, "code": warehouse.code, "name": warehouse.name},
                "source": source, "source_hash": fetched["source_hash"], "status_hash": fetched["status_hash"],
                "action": action, "order_revision": order.stock_revision, "policy_revision": policy.revision, "plan": plan}
    created = (await db.execute(insert(OrderStockPreview).values(
        id=preview_id, shop_id=shop.id, order_id=order.id, request_hash=request_hash, preview_hash=_hash(snapshot),
        preview_data=snapshot, status="completed" if result is not None else "prepared", created_at=at,
        expires_at=expires, completed_at=at if result is not None else None, result=result,
    ).on_conflict_do_nothing(index_elements=["id"]).returning(OrderStockPreview.id))).scalar_one_or_none()
    row = await db.get(OrderStockPreview, preview_id)
    if row.request_hash != request_hash:
        raise OrderStockError("order_stock_request_changed")
    response = _response(row)
    await db.commit()
    return response


async def apply(db: AsyncSession, preview_id: str, payload: OrderStockApplyRequest) -> dict:
    row = await db.get(OrderStockPreview, str(preview_id))
    if row is None:
        raise OrderStockError("order_stock_preview_not_found", 404)
    if row.preview_hash != payload.preview_hash:
        raise OrderStockError("order_stock_preview_changed")
    if row.status == "completed":
        return row.result
    snapshot = row.preview_data
    if now() >= row.expires_at:
        raise OrderStockError("order_stock_preview_expired")
    if _hash(snapshot) != row.preview_hash or not snapshot["plan"]["ready"]:
        raise OrderStockError("order_stock_preview_blocked")
    if snapshot["action"] == "issue" and payload.physical_confirmed is not True:
        raise OrderStockError("order_stock_physical_confirmation_required", 422)
    shop = await _shop(db, snapshot["shop_code"])
    fetched = await load_source(db, shop, snapshot["source"]["order_number"])
    # Network reads finish before row locks. The current source must match
    # exactly what the operator reviewed, including its update timestamp.
    if fetched["source_hash"] != snapshot["source_hash"] or fetched["status_hash"] != snapshot["status_hash"]:
        raise OrderStockError("order_stock_source_changed")
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": IDENTITY_WRITE_LOCK})
    shop = await _shop(db, shop.code, lock=True)
    policy = await _policy(db, shop.id, lock=True)
    warehouse = await _warehouse(db, policy.warehouse_id, lock=True)
    row = await db.scalar(select(OrderStockPreview).where(OrderStockPreview.id == str(preview_id))
                          .with_for_update().execution_options(populate_existing=True))
    if row.status == "completed":
        return row.result
    if row.preview_hash != payload.preview_hash or _hash(row.preview_data) != row.preview_hash:
        raise OrderStockError("order_stock_preview_changed")
    if now() >= row.expires_at:
        raise OrderStockError("order_stock_preview_expired")
    if policy.revision != snapshot["policy_revision"] or warehouse.id != snapshot["warehouse"]["id"]:
        raise OrderStockError("order_stock_policy_changed")
    source = fetched["order"]
    source["lines"] = await resolve_lines(db, shop, source)
    action = _action(policy, source, fetched["status_hash"], fetched["statuses"])
    if action != snapshot["action"]:
        raise OrderStockError("order_stock_source_changed")
    try:
        order = await ledger.get_or_create_order(db, shop, source, warehouse.id)
        if order.id != row.order_id or order.stock_revision != snapshot["order_revision"]:
            raise OrderStockError("order_stock_revision_changed")
        if order.stock_state == "issued":
            raise OrderStockError("order_stock_issued_locked")
        result = await ledger.apply_order(db, order, source, action, warehouse.id, snapshot["plan"])
    except ledger.LedgerError as error:
        raise OrderStockError(error.code, error.status) from None
    order.stock_source_hash = fetched["source_hash"]
    row.status, row.completed_at, row.result = "completed", now(), result
    await db.flush()
    await db.commit()
    return result
