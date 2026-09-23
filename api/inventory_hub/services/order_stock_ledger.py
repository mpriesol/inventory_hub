"""Local, transaction-scoped order reservations and immutable stock issues.

The orchestration layer owns identity/policy locks, source freshness, operator
confirmation, durable preview replay and commit. This module never calls a shop.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from inventory_hub.db_models import MovementType, OrderStatus, Product, ReservationStatus, Warehouse
from inventory_hub.db_models_ext import Reservation, ShopOrder, ShopOrderItem, StockBalance, StockMovement
from inventory_hub.services.stock_balances import lock_stock_balances
from inventory_hub.services.stock_publication_gate import StockPublicationHoldError


ZERO = Decimal("0")
MONEY = Decimal("0.0001")
MAX_QUANTITY = Decimal("999999999")
ACTIVE = {ReservationStatus.reserved, ReservationStatus.backorder}


class OrderStockError(Exception):
    def __init__(self, code: str, status: int = 409, errors: list | None = None):
        self.code, self.status, self.errors = code, status, errors or []
        super().__init__(code)


# Both names are kept for the orchestration and ledger API callers.
LedgerError = OrderStockError


def _number(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (ValueError, InvalidOperation):
        return None


def _text(value: Decimal) -> str:
    raw = format(value, "f")
    return raw.rstrip("0").rstrip(".") if "." in raw else raw


def _valid_uuid(value) -> bool:
    try:
        return isinstance(value, str) and str(UUID(value)) == value and UUID(value).int != 0
    except (ValueError, AttributeError):
        return False


def _error(code: str, **facts) -> dict:
    return {"code": "order_stock_" + code, **facts}


async def get_or_create_order(db, shop, source_order: dict, warehouse_id: int) -> ShopOrder:
    """Claim an order number; historical unmanaged orders are never adopted."""
    number, source_uuid = source_order.get("order_number"), source_order.get("uuid")
    if not isinstance(number, str) or not number or len(number) > 100 or not _valid_uuid(source_uuid):
        raise OrderStockError("order_stock_invalid_order")
    try:
        created_at = datetime.fromisoformat(source_order["created_at"].replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise OrderStockError("order_stock_invalid_order") from None
    try:
        # The savepoint keeps a conflicting UUID claim from poisoning the outer
        # transaction; the caller still rolls back the entire failed operation.
        async with db.begin_nested():
            await db.execute(insert(ShopOrder).values(
                shop_id=shop.id, external_id=number, external_code=number,
                order_date=created_at, status=OrderStatus.new, currency=None,
                stock_state="pending", stock_revision=0,
                stock_warehouse_id=warehouse_id, stock_source_uuid=source_uuid,
            ).on_conflict_do_nothing(index_elements=["shop_id", "external_id"]))
    except IntegrityError:
        raise OrderStockError("order_stock_order_identity_conflict") from None
    order = (await db.execute(select(ShopOrder).where(
        ShopOrder.shop_id == shop.id, ShopOrder.external_id == number,
    ).with_for_update().execution_options(populate_existing=True))).scalar_one()
    _validate_order(order, source_order, warehouse_id)
    return order


def _validate_order(order, source_order: dict, warehouse_id: int):
    if order.stock_state == "unmanaged":
        raise OrderStockError("order_stock_unmanaged_order")
    if order.stock_state not in {"pending", "reserved", "cancelled", "issued"}:
        raise OrderStockError("order_stock_invariant")
    if order.stock_warehouse_id != warehouse_id:
        raise OrderStockError("order_stock_warehouse_changed")
    if order.external_id != source_order.get("order_number") or order.stock_source_uuid != source_order.get("uuid"):
        raise OrderStockError("order_stock_order_identity_conflict")


def _source_lines(source_order: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """Strict writable piece quantities; excluded manual work never gets stock."""
    lines, excluded, errors, seen = [], [], [], set()
    for row in sorted(source_order.get("lines", []), key=lambda row: str(row.get("line_key", ""))):
        key = row.get("line_key")
        if not _valid_uuid(key) or key in seen:
            errors.append(_error("invalid_line_key", line_key=key))
            continue
        seen.add(key)
        classification = row.get("classification")
        if classification in {"manual", "non_stock"}:
            excluded.append({"line_key": key, "code": row.get("code", ""),
                             "title": row.get("title", ""), "classification": classification})
            continue
        if (row.get("identity_invalid") or not row.get("code")
                or not (classification == "mapped" or
                        (classification == "identified" and row.get("matched_by") == "shared_sku"))
                or type(row.get("product_id")) is not int or not row.get("sku")):
            errors.append(_error("line_identity", line_key=key))
            continue
        quantity = _number(row.get("quantity"))
        if quantity is None or not ZERO < quantity <= MAX_QUANTITY or quantity != quantity.to_integral_value():
            errors.append(_error("quantity_unsupported", line_key=key))
            continue
        if row.get("unit") != "ks" or row.get("length_unit") or row.get("length") not in (None, "", "0", "0.0", "0.00", "0.000"):
            errors.append(_error("unit_unsupported", line_key=key))
            continue
        lines.append({"line_key": key, "product_id": row["product_id"], "sku": row["sku"],
                      "code": row["code"], "quantity": quantity})
    return lines, excluded, errors


async def _stored(db, order_id: int):
    items = list((await db.execute(select(ShopOrderItem).where(
        ShopOrderItem.order_id == order_id, ShopOrderItem.stock_managed.is_(True),
    ).order_by(ShopOrderItem.id).execution_options(populate_existing=True))).scalars())
    reservations = list((await db.execute(select(Reservation).join(
        ShopOrderItem, ShopOrderItem.id == Reservation.shop_order_item_id,
    ).where(ShopOrderItem.order_id == order_id, ShopOrderItem.stock_managed.is_(True))
        .execution_options(populate_existing=True))).scalars())
    return {item.external_item_id: item for item in items}, {row.shop_order_item_id: row for row in reservations}


async def _plan(db, order, source_order, action, warehouse_id, *, lock):
    _validate_order(order, source_order, warehouse_id)
    if order.stock_state == "issued":
        raise OrderStockError("order_stock_issued_locked")
    if action not in {"reserve", "cancel", "issue"}:
        raise OrderStockError("order_stock_action_invalid", 422)
    items, reservations = await _stored(db, order.id)
    lines, excluded, errors = _source_lines(source_order) if action != "cancel" else ([], [], [])
    previous, old_by_line = defaultdict(lambda: ZERO), {}
    for item in items.values():
        reservation = reservations.get(item.id)
        if not reservation or reservation.status not in ACTIVE:
            continue
        quantity, shortage = _number(reservation.quantity), _number(reservation.shortage_qty)
        if (reservation.warehouse_id != warehouse_id or reservation.product_id != item.product_id
                or quantity is None or shortage is None or quantity <= ZERO or not ZERO <= shortage <= quantity):
            errors.append(_error("invariant", line_key=item.external_item_id))
            continue
        allocated = quantity - shortage
        previous[reservation.product_id] += allocated
        old_by_line[item.external_item_id] = allocated
    by_product = defaultdict(list)
    for line in lines:
        by_product[line["product_id"]].append(line)
    ids = set(previous) | set(by_product)
    if lock:
        warehouse = (await db.execute(select(Warehouse.id).where(
            Warehouse.id == warehouse_id,
        ).with_for_update(read=True))).scalar_one_or_none()
        if warehouse is None:
            raise OrderStockError("order_stock_warehouse_missing")
    query = select(Product).where(Product.id.in_(sorted(ids))).order_by(Product.id)
    if lock:
        query = query.with_for_update(read=True)
    products = {row.id: row for row in (await db.execute(query.execution_options(populate_existing=True))).scalars()}
    if lock:
        try:
            balances, _ = await lock_stock_balances(db, ids, warehouse_id, create_missing=False)
        except StockPublicationHoldError as error:
            raise OrderStockError(error.code, error.status) from None
    else:
        balances = {row.product_id: row for row in (await db.execute(select(StockBalance).where(
            StockBalance.product_id.in_(sorted(ids)), StockBalance.warehouse_id == warehouse_id,
        ).execution_options(populate_existing=True))).scalars()}
    effects, planned_lines = [], []
    for product_id in sorted(ids):
        product, balance = products.get(product_id), balances.get(product_id)
        current = by_product[product_id]
        if product is None or any(line["sku"] != product.sku for line in current):
            errors.append(_error("line_identity", product_id=product_id))
            continue
        if current and not product.is_active:
            errors.append(_error("inactive_product", product_id=product_id))
        on_hand = Decimal(balance.qty_on_hand) if balance else ZERO
        reserved = Decimal(balance.qty_reserved) if balance else ZERO
        avg = Decimal(balance.avg_cost) if balance else ZERO
        value = Decimal(balance.total_value) if balance else ZERO
        old = previous[product_id]
        if (not all(number.is_finite() for number in (on_hand, reserved, avg, value))
                or not ZERO <= old <= reserved <= on_hand or min(avg, value) < ZERO
                or (on_hand == ZERO and value != ZERO)):
            errors.append(_error("invariant", product_id=product_id))
            continue
        # Receiving rounds average and value independently to four decimals.
        # Allow that rounding residual, but do not consume materially
        # inconsistent acquisition values from a legacy balance.
        if action != "cancel" and abs(value - on_hand * avg) > (on_hand + 1) * MONEY / 2:
            errors.append(_error("invariant", product_id=product_id))
            continue
        if action != "cancel" and any(number != number.to_integral_value() for number in (on_hand, reserved, old)):
            errors.append(_error("unit_unsupported", product_id=product_id))
            continue
        demand = sum((line["quantity"] for line in current), ZERO)
        if demand > MAX_QUANTITY:
            errors.append(_error("quantity_unsupported", product_id=product_id))
            continue
        available = on_hand - reserved + old
        allocated = min(demand, available)
        shortage = demand - allocated
        if action == "issue" and shortage:
            errors.append(_error("insufficient_stock", product_id=product_id))
        # A failed issue has no projected partial dispatch. Its shortage is still
        # visible, but no physical movement is eligible for application.
        issue_quantity = demand if action == "issue" and shortage == ZERO else ZERO
        allocation_after = allocated if action == "reserve" else ZERO
        issue_cost = (value if issue_quantity == on_hand and issue_quantity > ZERO else
                      (value * issue_quantity / on_hand).quantize(MONEY, rounding=ROUND_HALF_UP)
                      if issue_quantity > ZERO else ZERO)
        effect = {"product_id": product_id, "sku": product.sku,
                  "qty_on_hand": on_hand, "qty_reserved": reserved, "avg_cost": avg, "total_value": value,
                  "old_allocation": old, "allocation": allocation_after, "shortage": shortage,
                  "issue_quantity": issue_quantity, "issue_cost": issue_cost,
                  "qty_on_hand_after": on_hand - issue_quantity,
                  "qty_reserved_after": reserved - old + allocation_after,
                  "total_value_after": value - issue_cost}
        effects.append({key: _text(val) if isinstance(val, Decimal) else val for key, val in effect.items()})
        remaining = allocated
        for line in current:
            line_allocated = min(line["quantity"], remaining)
            remaining -= line_allocated
            planned_lines.append({"line_key": line["line_key"], "product_id": product_id,
                "sku": product.sku, "quantity": _text(line["quantity"]),
                "old_allocation": _text(old_by_line.get(line["line_key"], ZERO)),
                "allocation": _text(line_allocated), "shortage": _text(line["quantity"] - line_allocated)})
    plan = {"action": action, "order_id": order.id, "order_revision": order.stock_revision,
            "warehouse_id": warehouse_id, "stock_state": order.stock_state,
            "ready": not errors, "errors": errors,
            "lines": sorted(planned_lines, key=lambda row: row["line_key"]),
            "excluded_lines": excluded, "effects": effects}
    return plan, items, reservations, balances, lines


async def plan_order(db, order, source_order: dict, action: str, warehouse_id: int, lock: bool = False) -> dict:
    """Return a deterministic plan; the default never creates a balance row."""
    return (await _plan(db, order, source_order, action, warehouse_id, lock=lock))[0]


async def apply_order(db, order, source_order: dict, action: str, warehouse_id: int, expected_plan: dict) -> dict:
    """Recheck the preview under locks, then stage all effects without commit."""
    # Callers normally already hold this lock. Refresh also protects direct
    # internal callers from a stale ORM revision or an already-issued order.
    order = (await db.execute(select(ShopOrder).where(ShopOrder.id == order.id)
        .with_for_update().execution_options(populate_existing=True))).scalar_one()
    plan, items, reservations, balances, source_lines = await _plan(
        db, order, source_order, action, warehouse_id, lock=True)
    if plan != expected_plan:
        raise OrderStockError("order_stock_preview_stale")
    if not plan["ready"]:
        raise OrderStockError("order_stock_plan_blocked", errors=plan["errors"])
    stamp = datetime.now(timezone.utc)
    current_keys = {line["line_key"] for line in source_lines}
    for key, item in items.items():
        if action == "cancel" or key not in current_keys:
            item.status = "cancelled" if action == "cancel" else "removed"
            reservation = reservations.get(item.id)
            if reservation and reservation.status in ACTIVE:
                reservation.status = ReservationStatus.cancelled
    for line in source_lines:
        item = items.get(line["line_key"])
        if item is None:
            item = ShopOrderItem(order_id=order.id, external_item_id=line["line_key"], stock_managed=True)
            items[line["line_key"]] = item
            db.add(item)
        item.product_id, item.quantity = line["product_id"], line["quantity"]
        item.external_product_code = line["code"]
        item.status = "fulfilled" if action == "issue" else "reserved"
    await db.flush()
    planned_by_key = {row["line_key"]: row for row in plan["lines"]}
    for line in source_lines:
        item = items[line["line_key"]]
        reservation = reservations.get(item.id)
        if reservation is None:
            reservation = Reservation(shop_order_item_id=item.id)
            reservations[item.id] = reservation
            db.add(reservation)
        projected = planned_by_key[line["line_key"]]
        reservation.product_id, reservation.warehouse_id = line["product_id"], warehouse_id
        reservation.quantity = line["quantity"]
        reservation.shortage_qty = Decimal(projected["shortage"])
        reservation.status = (ReservationStatus.fulfilled if action == "issue" else
                              ReservationStatus.backorder if reservation.shortage_qty else ReservationStatus.reserved)
        reservation.expires_at = None
        reservation.sale_movement_id = None
    movements = []
    for effect in plan["effects"]:
        product_id = effect["product_id"]
        balance = balances.get(product_id)
        if balance is None:
            # A zero-allocation backorder leaves the balance absent, preserving
            # eligibility for a later controlled opening-stock count.
            continue
        balance.qty_reserved = Decimal(effect["qty_reserved_after"])
        if action != "issue" or Decimal(effect["issue_quantity"]) == ZERO:
            continue
        remaining_quantity = Decimal(effect["issue_quantity"])
        remaining_cost = Decimal(effect["issue_cost"])
        running_balance = Decimal(effect["qty_on_hand"])
        for line in sorted((line for line in source_lines if line["product_id"] == product_id),
                           key=lambda line: line["line_key"]):
            quantity = line["quantity"]
            cost = (remaining_cost if quantity == remaining_quantity else
                    (remaining_cost * quantity / remaining_quantity).quantize(MONEY, rounding=ROUND_HALF_UP))
            remaining_quantity -= quantity
            remaining_cost -= cost
            running_balance -= quantity
            item = items[line["line_key"]]
            movement = StockMovement(
                idempotency_key=f"order:{order.id}:issue:{item.id}", product_id=product_id,
                warehouse_id=warehouse_id, movement_type=MovementType.SALE_OUT, quantity=-quantity,
                unit_cost=Decimal(effect["avg_cost"]), total_cost=cost,
                unit_cost_currency="EUR", fx_rate_to_eur=Decimal("1"),
                reference_type="shop_order", reference_id=str(order.id), reference_source=str(order.shop_id),
                balance_after=running_balance, avg_cost_after=Decimal(effect["avg_cost"]),
                created_by="order-stock", created_at=stamp,
            )
            db.add(movement)
            movements.append((movement, reservations[item.id], balance))
        balance.qty_on_hand = Decimal(effect["qty_on_hand_after"])
        balance.total_value = Decimal(effect["total_value_after"])
        balance.last_movement_at = stamp
    await db.flush()
    for movement, reservation, balance in movements:
        reservation.sale_movement_id = movement.id
        balance.last_movement_id = movement.id
    order.stock_state = {"reserve": "reserved", "cancel": "cancelled", "issue": "issued"}[action]
    order.stock_revision += 1
    order.stock_snapshot = source_order
    order.status = {"reserve": OrderStatus.processing, "cancel": OrderStatus.cancelled,
                    "issue": OrderStatus.completed}[action]
    result = {"order_id": order.id, "order_number": order.external_id, "action": action,
              "stock_state": order.stock_state, "revision": order.stock_revision,
              "movements_created": len(movements), "movement_ids": [row.id for row, _, _ in movements],
              "lines": plan["lines"], "excluded_lines": plan["excluded_lines"], "effects": plan["effects"]}
    if action == "issue":
        order.stock_issued_at = stamp
        order.stock_issue_result = result
    await db.flush()
    return result
