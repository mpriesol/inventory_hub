"""Native order effects are evidence, never receipts, reservations or API writes.

Only the documented system type Canceled releases the native obligation. Local
policy choices and names such as Storno have no effect on this calculation.
Callers hold the order/context locks and own the transaction.
"""
from collections import defaultdict
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
import re

from sqlalchemy import delete, func, select

from inventory_hub.db_models import Product
from inventory_hub.db_models_ext import ShopOrder, ShopProduct
from inventory_hub.order_collection_models import OrderInboxEntry
from inventory_hub.order_processing_models import OrderProcessingJob
from inventory_hub.order_stock_models import OrderStockPolicy
from inventory_hub.stock_native_models import StockNativeEvent, StockNativeLine, StockNativeOrder
from inventory_hub.stock_publication_models import StockPublicationHold

STATUS_TYPES = frozenset({"Received", "Canceled", "Sent", "PaymentSuccessful", "PaymentFailed", "PaymentCanceled",
    "PaymentInProcess", "Unresolved", "Custom", "HomecreditProcessing", "HomecreditRejected", "HomecreditApproved",
    "HomecreditReadyToShip", "HomecreditSent", "HomecreditDelivered", "HomecreditPaid", "HomecreditCanceled"})
SEMANTIC_ERRORS = frozenset({"stock_native_reopened_unknown", "stock_native_cancellation_unknown"})


class NativeError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def _quantity(value):
    try:
        if value is None or isinstance(value, bool):
            raise ValueError
        value = Decimal(str(value))
        if not value.is_finite() or not 0 < value <= 999999999 or value != value.to_integral_value():
            raise ValueError
        return value
    except (ValueError, InvalidOperation):
        raise NativeError("stock_native_quantity_unknown") from None


def facts(order, source, statuses):
    """Normalize full native demand, independent of allocated/local quantities."""
    if (source.get("uuid") != order.stock_source_uuid or source.get("order_number") != order.external_id
            or order.stock_warehouse_id is None or order.stock_state == "unmanaged"):
        raise NativeError("stock_native_order_identity")
    matches = [row for row in statuses if row.get("id") == source.get("status_id")]
    if len(matches) != 1 or matches[0].get("type") not in STATUS_TYPES:
        raise NativeError("stock_native_status_unknown")
    kind = matches[0]["type"]
    totals, lines, seen = defaultdict(lambda: Decimal(0)), [], set()
    for line in source.get("lines", []):
        key = line.get("line_key")
        if not key or key in seen:
            raise NativeError("stock_native_line_identity")
        seen.add(key)
        if line.get("classification") in ("manual", "non_stock"):
            service = (line.get("classification") == "non_stock" and line.get("item_type") == "service"
                       and type(line.get("product_id")) is int and not line.get("identity_invalid")
                       and "service_outside_stock" in line.get("reasons", []))
            if line.get("has_native_identity") and not service:
                raise NativeError("stock_native_line_identity")
            continue
        if (line.get("identity_invalid") or line.get("classification") not in ("mapped", "identified")
                or type(line.get("product_id")) is not int or not line.get("code")
                or line.get("unit") != "ks" or line.get("length_unit")
                or line.get("length") not in (None, "", "0", "0.0", "0.00", "0.000")):
            raise NativeError("stock_native_line_identity")
        quantity = _quantity(line.get("quantity"))
        native = line.get("has_native_identity")
        if type(native) is not bool:
            raise NativeError("stock_native_identity_unknown")
        product_id, option_id = line.get("native_product_id"), line.get("native_option_set_id")
        if native:
            if line.get("kind") != "product" or line.get("parent_uuid"):
                raise NativeError("stock_native_line_unsupported")
            if type(product_id) is not int or product_id <= 0 or (option_id is not None and
                    (type(option_id) is not int or option_id <= 0)):
                raise NativeError("stock_native_identity_unknown")
        elif product_id is not None or option_id is not None:
            raise NativeError("stock_native_line_identity")
        lines.append({"line_key": key, "product_id": line["product_id"], "code": line["code"],
                      "native_product_id": product_id, "native_option_set_id": option_id,
                      "quantity": str(int(quantity)), "native": native})
        if native:
            totals[line["product_id"]] -= quantity if kind != "Canceled" else Decimal(0)
    return {"facts_complete": True, "source_uuid": source["uuid"], "source_updated_at": source["updated_at"],
            "status_id": source["status_id"], "status_type": kind, "order_revision": order.stock_revision,
            "lines": sorted(lines, key=lambda row: row["line_key"]),
            "quantities": {str(key): str(int(value)) for key, value in sorted(totals.items())}}


def _native_lines(value):
    fields = ("line_key", "product_id", "code", "native_product_id", "native_option_set_id", "quantity")
    return sorted(tuple(line.get(key) for key in fields) for line in value["lines"] if line.get("native"))


async def _identities(db, shop_id, value, *, bind=False):
    native = [line for line in value["lines"] if line["native"]]
    if not native:
        return
    rows = (await db.scalars(select(ShopProduct).where(ShopProduct.shop_id == shop_id,
        ShopProduct.product_id.in_({line["product_id"] for line in native})))).all()
    mappings = {row.product_id: row for row in rows}
    aliases = None
    if any((mapping := mappings.get(line["product_id"])) is not None
           and (mapping.external_code) != line["code"] for line in native):
        from inventory_hub.db_models import Shop
        from inventory_hub.services import order_sku_aliases
        from inventory_hub.services.product_identity import load_identity_index
        shop = await db.get(Shop, shop_id)
        index = await load_identity_index(db, shop_id, [order_sku_aliases.identity(shop_id, line) for line in native])
        aliases = await order_sku_aliases.load(db, shop, index, native)
    for line in native:
        mapping = mappings.get(line["product_id"])
        if mapping is None:
            raise NativeError("stock_native_identity_unknown")
        code = mapping.external_code
        alias_proof = None
        if code != line["code"]:
            resolved, alias_proof = aliases.resolve({**line, "has_native_identity": True})
            if resolved.status != "mapped" or resolved.product_id != line["product_id"] or alias_proof is None:
                raise NativeError("stock_native_line_identity")
        # option_set_id is preserved separately; an unverified equivalence to
        # product.variant_id must never be manufactured by this evidence layer.
        if mapping.is_variant:
            if line["native_option_set_id"] is None:
                raise NativeError("stock_native_identity_unknown")
            if mapping.external_product_id is not None and str(line["native_product_id"]) != mapping.external_product_id:
                raise NativeError("stock_native_line_identity")
        elif (line["native_option_set_id"] is not None or mapping.native_id is None
              or str(line["native_product_id"]) != mapping.native_id):
            raise NativeError("stock_native_line_identity")
        proof = {"id": mapping.id, "code": code, "parent_code": mapping.parent_code,
                 "external_id": mapping.native_id, "is_variant": mapping.is_variant}
        stored = line.get("mapping")
        # An audited code-only rename must not invalidate an immutable older
        # observation. Native leaf, mapping row and variant parent stay fixed.
        same_native = (alias_proof is not None and isinstance(stored, dict)
                       and stored.get("code") in alias_proof["code_chain"]
                       and all(stored.get(key) == proof[key] for key in ("id", "external_id", "is_variant", "parent_code")))
        if (stored is not None and stored != proof and not same_native) or (not bind and stored is None):
            raise NativeError("stock_native_line_identity")
        if bind:
            line["mapping"] = proof
            if alias_proof is not None:
                line["sku_alias_proof"] = alias_proof


async def record(db, order, source_order, statuses, source_hash):
    if not isinstance(source_hash, str) or re.fullmatch(r"[0-9a-f]{64}", source_hash) is None:
        raise NativeError("stock_native_source_unknown")
    await db.scalar(select(ShopOrder.id).where(ShopOrder.id == order.id).with_for_update())
    current = await db.scalar(select(StockNativeOrder).where(StockNativeOrder.order_id == order.id)
                              .with_for_update().execution_options(populate_existing=True))
    error = None
    try:
        value = facts(order, source_order, statuses)
        await _identities(db, order.shop_id, value, bind=True)
    except NativeError as failure:
        error = failure.code
        value = {"facts_complete": False, "facts_error": error,
                 "source_uuid": source_order.get("uuid"), "source_updated_at": source_order["updated_at"],
                 "status_id": source_order["status_id"], "status_type": next((row["type"] for row in statuses
                    if row["id"] == source_order["status_id"]), "Unknown"),
                 "order_revision": order.stock_revision, "lines": [], "quantities": {}}
    updated = datetime.fromisoformat(value["source_updated_at"])
    if updated.tzinfo is None:
        raise NativeError("stock_native_source_unknown")
    if current:
        previous = await db.get(StockNativeEvent, current.event_id)
        if current.shop_id != order.shop_id or current.warehouse_id != order.stock_warehouse_id:
            raise NativeError("stock_native_order_identity")
        if updated < current.source_updated_at:
            error = "stock_native_source_behind"
        if current.canceled_seen and value["status_type"] != "Canceled":
            error = "stock_native_reopened_unknown"
        if (value["status_type"] == "Canceled" and
                (current.error is not None or _native_lines(previous.facts) != _native_lines(value))):
            # If quantities changed between our active snapshot and cancellation,
            # their edit/cancel ordering is not proven either. Edits after an
            # observed cancellation likewise have no documented native contract.
            error = "stock_native_cancellation_unknown"
        # Unsupported native transitions cannot be cleared by another ordinary
        # refresh. Their intermediate effect needs controlled reconciliation.
        if current.error in SEMANTIC_ERRORS:
            error = current.error
        if value.get("facts_complete") is not True:
            error = value["facts_error"]
        previous_value = {key: item for key, item in previous.facts.items() if key != "maintenance"}
        if previous.source_hash == source_hash and previous_value == value and previous.error == error:
            return current
    elif value["status_type"] == "Canceled":
        error = "stock_native_cancellation_unknown"
    if value.get("facts_complete") is not True:
        error = value["facts_error"]
    revision = current.revision + 1 if current else 1
    event = StockNativeEvent(order_id=order.id, revision=revision, source_hash=source_hash, facts=value, error=error)
    db.add(event)
    await db.flush()
    if current is None:
        current = StockNativeOrder(order_id=order.id, shop_id=order.shop_id,
                                   warehouse_id=order.stock_warehouse_id, canceled_seen=False)
        db.add(current)
    current.revision, current.order_revision, current.event_id = revision, order.stock_revision, event.id
    current.source_hash = source_hash
    current.source_updated_at = max(current.source_updated_at, updated) if current.source_updated_at else updated
    current.status_id, current.status_type = value["status_id"], value["status_type"]
    current.canceled_seen = current.canceled_seen or value["status_type"] == "Canceled"
    current.error = error
    await db.flush()
    await db.execute(delete(StockNativeLine).where(StockNativeLine.order_id == order.id))
    if error is None:
        db.add_all([StockNativeLine(order_id=order.id, product_id=int(key), quantity=Decimal(amount))
                    for key, amount in value["quantities"].items()])
    await db.flush()
    return current


async def coverage(db, shop_id, warehouse_id, *, maintenance_plan=None):
    """Require evidence for every managed order and its latest accounted source.

The collector's historical coverage contract is a separate activation gate;
absence of an inbox row is not proof that pre-cutover orders cannot mutate.
"""
    allowed = {}
    if maintenance_plan is not None:
        if (maintenance_plan.get("shop_id") != shop_id or maintenance_plan.get("warehouse_id") != warehouse_id
                or maintenance_plan != await _maintenance_plan(db, shop_id, warehouse_id, maintenance_plan.get("skus"))):
            raise NativeError("stock_native_reconciliation_changed")
        allowed = {row["order_id"]: row for row in maintenance_plan["orders"]}
    scope = (ShopOrder.shop_id == shop_id, ShopOrder.stock_warehouse_id == warehouse_id,
             ShopOrder.stock_state != "unmanaged")
    rows = (await db.execute(select(ShopOrder, StockNativeOrder, StockNativeEvent).select_from(ShopOrder).outerjoin(StockNativeOrder,
        StockNativeOrder.order_id == ShopOrder.id).outerjoin(StockNativeEvent,
        StockNativeEvent.id == StockNativeOrder.event_id).where(*scope))).all()
    for order, current, event in rows:
        if current is None:
            raise NativeError("stock_native_coverage_missing")
        permitted = current.order_id in allowed and current.error in SEMANTIC_ERRORS
        if current.error and not permitted:
            raise NativeError(current.error)
        if (current.shop_id != shop_id or current.warehouse_id != warehouse_id
                or current.order_revision != order.stock_revision or event is None
                or event.source_hash != current.source_hash or event.revision != current.revision
                or event.order_id != order.id or (event.error is not None and not permitted)):
            raise NativeError("stock_native_coverage_stale")
        await _identities(db, shop_id, event.facts)
    policy = await db.get(OrderStockPolicy, shop_id)
    if policy is None or policy.warehouse_id != warehouse_id:
        raise NativeError("stock_native_coverage_missing")
    entries = (await db.execute(select(OrderInboxEntry, OrderProcessingJob, StockNativeOrder).select_from(OrderInboxEntry)
        .outerjoin(OrderProcessingJob, (OrderProcessingJob.shop_id == OrderInboxEntry.shop_id)
                   & (OrderProcessingJob.source_uuid == OrderInboxEntry.source_uuid))
        .outerjoin(ShopOrder, (ShopOrder.shop_id == OrderInboxEntry.shop_id)
                   & (ShopOrder.stock_source_uuid == OrderInboxEntry.source_uuid))
        .outerjoin(StockNativeOrder, StockNativeOrder.order_id == ShopOrder.id)
        .where(OrderInboxEntry.shop_id == shop_id, OrderInboxEntry.created_at >= policy.starts_at))).all()
    for entry, job, current in entries:
        if entry.deleted:
            raise NativeError("stock_native_deleted_unknown")
        if (entry.review_reason or current is None
                or current.source_updated_at < entry.updated_at
                or (current.source_updated_at == entry.updated_at and current.status_id != entry.status_id)):
            raise NativeError("stock_native_coverage_stale")
        # A confirmed manual apply already records the exact source and local
        # revision. It does not require manufacturing a processor job. Existing
        # publication order-readiness gates remain independently authoritative.
        if job is not None and (not job.source_hash or job.status not in ("completed", "retry")
                                or current.source_hash != job.source_hash):
            raise NativeError("stock_native_coverage_stale")
    return True


def _known_current_quantities(value):
    """Rebase current known demand, never reconstruct an uncertain past effect."""
    if value.get("facts_complete") is not True or value.get("status_type") not in STATUS_TYPES:
        raise NativeError("stock_native_reconciliation_unknown")
    amounts = defaultdict(lambda: Decimal(0))
    for line in value.get("lines", []):
        if type(line.get("product_id")) is not int or type(line.get("native")) is not bool:
            raise NativeError("stock_native_reconciliation_unknown")
        quantity = _quantity(line.get("quantity"))
        if line["native"]:
            amounts[line["product_id"]] -= quantity if value["status_type"] != "Canceled" else Decimal(0)
    result = {str(key): str(int(amount)) for key, amount in sorted(amounts.items())}
    if result != value.get("quantities"):
        raise NativeError("stock_native_reconciliation_unknown")
    return result


async def _maintenance_plan(db, shop_id, warehouse_id, skus):
    if (not isinstance(skus, list) or not 1 <= len(skus) <= 100
            or any(not isinstance(sku, str) or not 1 <= len(sku) <= 100 or sku != sku.strip()
                   or any(ord(char) < 32 or ord(char) == 127 for char in sku) for sku in skus)
            or len(set(skus)) != len(skus)):
        raise NativeError("stock_native_reconciliation_scope")
    selected = dict((await db.execute(select(Product.id, Product.sku).where(Product.sku.in_(skus)))).all())
    if len(selected) != len(skus):
        raise NativeError("stock_native_reconciliation_scope")
    rows = (await db.execute(select(StockNativeOrder, StockNativeEvent, ShopOrder)
        .select_from(StockNativeOrder).join(StockNativeEvent, StockNativeEvent.id == StockNativeOrder.event_id)
        .join(ShopOrder, ShopOrder.id == StockNativeOrder.order_id)
        .where(StockNativeOrder.shop_id == shop_id, StockNativeOrder.warehouse_id == warehouse_id,
               StockNativeOrder.error.is_not(None)).order_by(StockNativeOrder.order_id))).all()
    orders = []
    for current, event, order in rows:
        if current.error not in SEMANTIC_ERRORS:
            raise NativeError(current.error)
        if (event.order_id != order.id or event.revision != current.revision or event.error != current.error
                or event.source_hash != current.source_hash or current.order_revision != order.stock_revision
                or order.shop_id != shop_id or order.stock_warehouse_id != warehouse_id):
            raise NativeError("stock_native_reconciliation_changed")
        _known_current_quantities(event.facts)
        await _identities(db, shop_id, event.facts)
        history = (await db.scalars(select(StockNativeEvent).where(StockNativeEvent.order_id == order.id)
                                   .order_by(StockNativeEvent.revision))).all()
        affected = set()
        for previous in history:
            if previous.error is not None and previous.error not in SEMANTIC_ERRORS:
                raise NativeError("stock_native_reconciliation_unknown")
            _known_current_quantities(previous.facts)
            affected.update(line["product_id"] for line in previous.facts["lines"])
        if not affected <= selected.keys():
            raise NativeError("stock_native_reconciliation_scope")
        orders.append({"order_id": order.id, "event_id": current.event_id, "revision": current.revision,
            "order_revision": current.order_revision, "source_hash": current.source_hash,
            "error": current.error, "product_ids": sorted(affected), "facts": deepcopy(event.facts)})
    return {"shop_id": shop_id, "warehouse_id": warehouse_id, "skus": sorted(skus), "orders": orders}


async def maintenance_plan(db, shop_id, warehouse_id, skus):
    """Read-only exact scope for absorbing an uncertain native past at baseline."""
    with db.no_autoflush:
        return await _maintenance_plan(db, shop_id, warehouse_id, skus)


async def apply_maintenance(db, plan, hold_id):
    """Stage audited reconciliation after the caller verifies all remote counts.

No commit and no network calls. The caller must hold its identity/warehouse
context and commit this with every affected checkpoint, or roll everything back.
"""
    hold = await db.scalar(select(StockPublicationHold).where(StockPublicationHold.id == str(hold_id))
                          .with_for_update().execution_options(populate_existing=True))
    if (hold is None or not hold.active or hold.shop_id != plan.get("shop_id")
            or hold.warehouse_id != plan.get("warehouse_id")
            or any(hold.assertions.get(key) is not True for key in
                   ("external_writers_paused", "orders_reconciled", "confirmed"))):
        raise NativeError("stock_native_reconciliation_hold_required")
    ids = [row["order_id"] for row in plan.get("orders", [])]
    if ids:
        await db.execute(select(ShopOrder.id).where(ShopOrder.id.in_(ids)).order_by(ShopOrder.id).with_for_update())
        await db.execute(select(StockNativeOrder.order_id).where(StockNativeOrder.order_id.in_(ids))
                         .order_by(StockNativeOrder.order_id).with_for_update())
    fresh = await _maintenance_plan(db, plan["shop_id"], plan["warehouse_id"], plan.get("skus"))
    if fresh != plan:
        raise NativeError("stock_native_reconciliation_changed")
    await coverage(db, plan["shop_id"], plan["warehouse_id"], maintenance_plan=plan)
    for frozen in plan["orders"]:
        current = await db.get(StockNativeOrder, frozen["order_id"])
        value = deepcopy(frozen["facts"])
        value["maintenance"] = {"kind": "baseline_reconciliation", "hold_id": hold.id,
            "previous_event_id": frozen["event_id"], "previous_revision": frozen["revision"],
            "previous_error": frozen["error"], "skus": plan["skus"]}
        event = StockNativeEvent(order_id=current.order_id, revision=current.revision + 1,
                                 source_hash=current.source_hash, facts=value, error=None)
        db.add(event)
        await db.flush()
        current.event_id, current.revision, current.error = event.id, event.revision, None
        current.canceled_seen = current.status_type == "Canceled"
        await db.execute(delete(StockNativeLine).where(StockNativeLine.order_id == current.order_id))
        db.add_all([StockNativeLine(order_id=current.order_id, product_id=int(key), quantity=Decimal(amount))
            for key, amount in _known_current_quantities(value).items()])
    await db.flush()
    return {"hold_id": hold.id, "order_ids": ids}


async def projection(db, shop_id, warehouse_id, product_id, *, checked=False):
    if not checked:
        await coverage(db, shop_id, warehouse_id)
    value = await db.scalar(select(func.coalesce(func.sum(StockNativeLine.quantity), 0))
        .join(StockNativeOrder, StockNativeOrder.order_id == StockNativeLine.order_id)
        .where(StockNativeOrder.shop_id == shop_id, StockNativeOrder.warehouse_id == warehouse_id,
               StockNativeLine.product_id == product_id))
    return Decimal(value)
