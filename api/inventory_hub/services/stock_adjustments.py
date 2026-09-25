"""Physical recounts append compensating movements using the existing FIFO engine."""
from datetime import datetime, timedelta
from decimal import Decimal
from sqlalchemy import select, text
from inventory_hub.db_models import MovementType
from inventory_hub.db_models_ext import StockMovement
from inventory_hub.stock_adjustment_models import StockAdjustment
from inventory_hub.services import fifo
from inventory_hub.services import stock_tracking
from inventory_hub.services.stock_balances import lock_stock_balances
from inventory_hub.services.stock_publication_gate import StockPublicationHoldError


def dto(batch):
    return {"id": batch.id, "status": batch.status, "preview_hash": batch.preview_hash,
            "preview": batch.preview_data, "expires_at": batch.expires_at.isoformat(), "result": batch.result}


async def get(db, identifier):
    batch = await db.get(StockAdjustment, str(identifier))
    if batch is None:
        raise fifo.FifoError("adjustment_not_found", 404)
    return dto(batch)


def validate_count(balance, counted, *, initial_zero=False):
    current = Decimal(balance.qty_on_hand) if balance else fifo.ZERO
    reserved = Decimal(balance.qty_reserved) if balance else fifo.ZERO
    quarantined = Decimal(balance.qty_quarantined) if balance else fifo.ZERO
    if any(value < 0 or value != value.to_integral_value() for value in (current, reserved, quarantined)):
        raise fifo.FifoError("adjustment_quantity_invalid")
    if counted < reserved + quarantined:
        raise fifo.FifoError("adjustment_below_committed")
    if counted == current and not (initial_zero and counted == fifo.ZERO):
        raise fifo.FifoError("adjustment_no_change", 422)
    return counted - current


async def initial_zero_count(db, product, warehouse, balance, snapshot, counted):
    return counted == fifo.ZERO and not snapshot["tracking_confirmed"]


async def preview(db, payload):
    raw = payload.model_dump(mode="json")
    request_hash = fifo.digest(raw)
    # Serialize UUID recovery even before the first balance exists. This lock
    # precedes warehouse/product locks and is never held across external I/O.
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": int(fifo.digest(["stock-adjustment", str(payload.request_id)])[:15], 16)})
    old = await db.get(StockAdjustment, str(payload.request_id))
    if old:
        if old.request_hash != request_hash:
            raise fifo.FifoError("fifo_request_conflict")
        result = dto(old)
        await db.commit()
        return result
    product, warehouse, balance = await fifo._product_scope(db, sku=payload.sku,
        warehouse_code=payload.warehouse_code, lock=True)
    at = fifo.now()
    if payload.counted_at > at:
        raise fifo.FifoError("fifo_date_invalid", 422)
    snapshot = await fifo._snapshot(db, product, warehouse, balance)
    if not snapshot["tracking_confirmed"]:
        await stock_tracking.validate_start(db, balance)
    if snapshot["tracking_confirmed"] and snapshot["valuation"]["mode"] != "fifo" and (snapshot["movement_count"] or balance and balance.qty_on_hand != fifo.ZERO):
        raise fifo.FifoError("fifo_cutover_required")
    initial_zero = await initial_zero_count(db, product, warehouse, balance, snapshot, Decimal(payload.counted_quantity))
    delta = validate_count(balance if snapshot["tracking_confirmed"] else None, Decimal(payload.counted_quantity), initial_zero=initial_zero)
    if delta <= 0 and (payload.unit_cost is not None or payload.cost_status != "unknown"):
        raise fifo.FifoError("adjustment_issue_cost_derived", 422)
    plan = await fifo.plan_issue(db, balance, [{"line_key": "adjustment", "quantity": -delta}], lock=True) if delta < 0 else None
    if delta > 0:
        from types import SimpleNamespace
        layers = await fifo._layers(db, balance) if balance else []
        fifo.summarize([*layers, SimpleNamespace(quantity_remaining=delta,
            unit_cost=Decimal(payload.unit_cost) if payload.unit_cost is not None else None,
            cost_status=payload.cost_status, stock_status="available")])
    data = {**raw, "product_id": product.id, "warehouse_id": warehouse.id,
            "before_quantity": None if not snapshot["tracking_confirmed"] else fifo.number(balance.qty_on_hand if balance else fifo.ZERO),
            "starts_tracking": not snapshot["tracking_confirmed"],
            "initial_zero_count": initial_zero,
            "delta": fifo.number(delta), "snapshot_hash": fifo.digest(snapshot), "snapshot": snapshot,
            "issue_plan": plan, "currency": "EUR", "vat_included": False}
    batch = StockAdjustment(id=str(payload.request_id), product_id=product.id, warehouse_id=warehouse.id,
        request_hash=request_hash, preview_hash=fifo.digest(data), preview_data=data,
        status="prepared", created_at=at, expires_at=at + timedelta(minutes=30))
    db.add(batch)
    await db.flush()
    result = dto(batch)
    await db.commit()
    return result


async def apply(db, identifier, payload):
    original = await db.get(StockAdjustment, str(identifier))
    if original is None:
        raise fifo.FifoError("adjustment_not_found", 404)
    if original.preview_hash != payload.preview_hash:
        raise fifo.FifoError("fifo_preview_changed")
    if original.status == "completed":
        return original.result
    product, warehouse, balance = await fifo._product_scope(db, product_id=original.product_id,
        warehouse_code=original.preview_data["warehouse_code"], lock=True)
    batch = await db.scalar(select(StockAdjustment).where(StockAdjustment.id == original.id)
        .with_for_update().execution_options(populate_existing=True))
    if batch.status == "completed":
        return batch.result
    if batch.expires_at <= fifo.now():
        raise fifo.FifoError("fifo_preview_expired")
    data = batch.preview_data
    snapshot = await fifo._snapshot(db, product, warehouse, balance)
    if fifo.digest(data) != batch.preview_hash or fifo.digest(snapshot) != data["snapshot_hash"]:
        raise fifo.FifoError("fifo_preview_stale")
    initial_zero = await initial_zero_count(db, product, warehouse, balance, snapshot, Decimal(data["counted_quantity"]))
    if bool(data.get("initial_zero_count")) != initial_zero:
        raise fifo.FifoError("fifo_preview_stale")
    delta = validate_count(balance if snapshot["tracking_confirmed"] else None, Decimal(data["counted_quantity"]), initial_zero=initial_zero)
    was_missing = balance is None
    try:
        balances, created_ids = await lock_stock_balances(db, {product.id}, warehouse.id)
    except StockPublicationHoldError as error:
        raise fifo.FifoError(error.code, error.status) from None
    if was_missing and product.id not in created_ids:
        raise fifo.FifoError("fifo_preview_stale")
    balance = balances[product.id]
    await stock_tracking.activate(db, balance, source_type="stock_adjustment", source_id=batch.id,
                                 operator_name=data["operator_name"])
    if initial_zero:
        # A confirmed absence is evidence, not a receipt or a zero-cost product.
        # Keep the immutable movement ledger's nonzero invariant and create no layer.
        state = await fifo.ensure_fifo(db, balance, "initial_zero_count")
        if state is None or await fifo._layers(db, balance):
            raise fifo.FifoError("fifo_preview_stale")
        state.activation_kind = "initial_zero_count"
        # Starting tracking already advances the state revision; the registry also
        # invalidates every preview prepared before the confirmed zero count.
        balance.avg_cost, balance.total_value = fifo.ZERO, fifo.ZERO
        await db.flush()
        result = {"adjustment_id": batch.id, "movement_id": None, "initial_zero_count": True,
            "product_id": product.id, "warehouse_id": warehouse.id, "before_quantity": None,
            "counted_quantity": "0", "delta": "0", "valuation": await fifo.valuation(db, balance),
            "completed_at": fifo.now().isoformat()}
        batch.status, batch.completed_at, batch.result = "completed", fifo.now(), result
        await db.flush()
        await db.commit()
        return result
    movement = StockMovement(idempotency_key=f"stock-adjustment:{batch.id}", product_id=product.id,
        warehouse_id=warehouse.id, movement_type=MovementType.ADJUSTMENT_IN if delta > 0 else MovementType.ADJUSTMENT_OUT,
        quantity=delta, unit_cost_currency="EUR", fx_rate_to_eur=Decimal("1"),
        reference_type="stock_adjustment", reference_id=batch.id, reference_source=data["source_reference"][:100],
        notes=data["reason"], created_by=data["operator_name"], created_at=fifo.now())
    if delta > 0:
        cost = Decimal(data["unit_cost"]) if data["unit_cost"] is not None else None
        projected = await fifo.receipt_valuation(db, balance, delta, cost, data["cost_status"])
        if projected is None:
            raise fifo.FifoError("fifo_cutover_required")
        movement.unit_cost = movement.unit_cost_original = cost
        movement.total_cost = fifo.money(delta * cost) if cost is not None else None
        movement.balance_after = balance.qty_on_hand + delta
        movement.avg_cost_after = Decimal(projected["avg_cost"]) if projected["avg_cost"] is not None else None
        db.add(movement)
        await db.flush()
        balance.qty_on_hand += delta
        await fifo.add_receipt(db, balance, movement,
            datetime.fromisoformat(data["counted_at"].replace("Z", "+00:00")), cost, data["cost_status"],
            {"kind": "physical_adjustment", "adjustment_id": batch.id, "source_reference": data["source_reference"],
             "reason": data["reason"], "operator_name": data["operator_name"]})
    else:
        await fifo.apply_issue(db, balance, movement, data["issue_plan"]["allocations"])
    balance.last_movement_at, balance.last_movement_id = movement.created_at, movement.id
    result = {"adjustment_id": batch.id, "movement_id": movement.id, "initial_zero_count": False, "product_id": product.id,
              "warehouse_id": warehouse.id, "before_quantity": data["before_quantity"],
              "counted_quantity": data["counted_quantity"], "delta": fifo.number(delta),
              "valuation": await fifo.valuation(db, balance), "completed_at": fifo.now().isoformat()}
    batch.status, batch.completed_at, batch.result = "completed", fifo.now(), result
    await db.flush()
    await db.commit()
    return result
