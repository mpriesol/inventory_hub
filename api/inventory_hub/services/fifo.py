"""Physical FIFO layers, explicit legacy cutover, and auditable issue allocation.

Internal accounting functions require the caller's warehouse gate and balance
row lock and never commit. Public cutover operations own their transaction.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from sqlalchemy import func, select
from inventory_hub.db_models import MovementType, Product, Warehouse
from inventory_hub.db_models_ext import StockBalance, StockMovement
from inventory_hub.fifo_models import FifoState, FifoLayer, FifoAllocation, FifoCutover, FifoReceipt
from inventory_hub.services.stock_balances import lock_stock_balances
from inventory_hub.services.stock_publication_gate import StockPublicationHoldError
from inventory_hub.services import stock_tracking

ZERO = Decimal("0")
MONEY = Decimal("0.0001")
QTY = Decimal("0.001")
MAX_QUANTITY = Decimal("999999999.999")
MAX_VALUE = Decimal("999999999999.9999")
MAX_UNIT_COST = Decimal("99999999.9999")


class FifoError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def now():
    return datetime.now(timezone.utc)


def money(value):
    return Decimal(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def number(value):
    if value is None:
        return None
    value = format(Decimal(value), "f")
    return value.rstrip("0").rstrip(".") if "." in value else value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()


def allocation_cost(quantity_before, quantity, unit_cost):
    if unit_cost is None:
        return None
    return money(Decimal(quantity_before) * Decimal(unit_cost)) - money((Decimal(quantity_before) - Decimal(quantity)) * Decimal(unit_cost))


async def lock_state(db, balance):
    return await db.scalar(select(FifoState).where(FifoState.product_id == balance.product_id,
        FifoState.warehouse_id == balance.warehouse_id).with_for_update().execution_options(populate_existing=True))


async def ensure_fifo(db, balance, activation_kind, previous_qty=None, excluded_movement_id=None):
    if not await stock_tracking.is_confirmed(db, balance.product_id, balance.warehouse_id):
        raise FifoError("stock_tracking_required")
    state = await lock_state(db, balance)
    if state is not None:
        return state
    previous = Decimal(balance.qty_on_hand if previous_qty is None else previous_qty)
    if previous != ZERO:
        return None
    query = select(StockMovement.id).where(StockMovement.product_id == balance.product_id,
                                           StockMovement.warehouse_id == balance.warehouse_id,
                                           stock_tracking.current_movement())
    if excluded_movement_id is not None:
        query = query.where(StockMovement.id != excluded_movement_id)
    if await db.scalar(query.limit(1)) is not None:
        return None
    state = FifoState(product_id=balance.product_id, warehouse_id=balance.warehouse_id,
                      revision=1, activated_at=now(), activation_kind=activation_kind)
    db.add(state)
    await db.flush()
    return state


async def _layers(db, balance, lock=False):
    query = select(FifoLayer).where(FifoLayer.product_id == balance.product_id,
                                    FifoLayer.warehouse_id == balance.warehouse_id,
                                    stock_tracking.current_layer()).order_by(FifoLayer.id)
    if lock:
        query = query.with_for_update()
    return list((await db.scalars(query.execution_options(populate_existing=True))).all())


def summarize(layers, revision=0):
    known = provisional = unknown_qty = provisional_qty = quarantined = quantity = ZERO
    for layer in layers:
        remaining = Decimal(layer.quantity_remaining)
        quantity += remaining
        if layer.stock_status == "quarantine":
            quarantined += remaining
        if layer.cost_status == "unknown":
            unknown_qty += remaining
        elif layer.cost_status == "provisional":
            provisional_qty += remaining
            provisional += money(remaining * layer.unit_cost)
        else:
            known += money(remaining * layer.unit_cost)
    if quantity > MAX_QUANTITY or known + provisional > MAX_VALUE:
        raise FifoError("fifo_value_out_of_range")
    covered = unknown_qty == ZERO
    total = known + provisional if covered else None
    avg = money(total / quantity) if covered and quantity else ZERO if covered else None
    if avg is not None and avg > MAX_UNIT_COST:
        raise FifoError("fifo_value_out_of_range")
    return {"mode": "fifo", "revision": revision, "quantity": number(quantity),
            "known_value": number(known), "provisional_value": number(provisional),
            "unknown_qty": number(unknown_qty), "provisional_qty": number(provisional_qty),
            "quarantined_qty": number(quarantined), "value_complete": covered and provisional_qty == ZERO,
            "avg_cost": number(avg), "total_value": number(total)}


async def valuation(db, balance, lock=False):
    if balance is None or not await stock_tracking.is_confirmed(db, balance.product_id, balance.warehouse_id):
        return {"mode": "missing" if balance is None else "unconfirmed", "revision": 0, "quantity": None, "known_value": None,
                "provisional_value": None, "unknown_qty": None, "provisional_qty": None,
                "quarantined_qty": None, "value_complete": False, "avg_cost": None, "total_value": None, "next_layer": None, "last_purchase_cost_status": "unknown"}
    query = select(FifoState).where(FifoState.product_id == balance.product_id, FifoState.warehouse_id == balance.warehouse_id)
    if lock:
        query = query.with_for_update()
    state = await db.scalar(query.execution_options(populate_existing=True))
    if state is None:
        return {"mode": "legacy", "revision": 0, "quantity": number(balance.qty_on_hand),
                "known_value": number(balance.total_value), "provisional_value": "0", "unknown_qty": "0",
                "provisional_qty": "0", "quarantined_qty": number(balance.qty_quarantined),
                "value_complete": balance.avg_cost is not None and balance.total_value is not None,
                "avg_cost": number(balance.avg_cost), "total_value": number(balance.total_value), "next_layer": None,
                "last_purchase_cost_status": "legacy" if balance.last_purchase_price is not None else "unknown"}
    layers = await _layers(db, balance, lock)
    result = summarize(layers, state.revision)
    next_layer = next(iter(sorted((row for row in layers if row.stock_status == "available" and row.quantity_remaining > ZERO),
                                 key=lambda row: (row.physical_received_at, row.id))), None)
    latest_purchase = next(iter(sorted((row for row in layers if row.root_cost_layer_id == row.id
        and row.provenance.get("kind") in ("receiving", "documented_receipt")),
        key=lambda row: (row.physical_received_at, row.id), reverse=True)), None)
    result["last_purchase_cost_status"] = (latest_purchase.cost_status if latest_purchase is not None
        and (balance.last_purchase_at is None or balance.last_purchase_at <= latest_purchase.physical_received_at)
        else "legacy" if balance.last_purchase_price is not None else "unknown")
    result["next_layer"] = ({"id": next_layer.id, "unit_cost": number(next_layer.unit_cost),
        "cost_status": next_layer.cost_status, "physical_received_at": next_layer.physical_received_at.isoformat()}
        if next_layer else None)
    if Decimal(result["quantity"]) != balance.qty_on_hand or Decimal(result["quarantined_qty"]) != balance.qty_quarantined:
        raise FifoError("fifo_layer_balance_mismatch")
    return result


async def revalue(db, balance, state=None):
    if not await stock_tracking.is_confirmed(db, balance.product_id, balance.warehouse_id):
        raise FifoError("stock_tracking_required")
    await db.flush()
    if state is None:
        state = await lock_state(db, balance)
    if state is None:
        raise FifoError("fifo_cutover_required")
    result = summarize(await _layers(db, balance, lock=True), state.revision)
    if Decimal(result["quantity"]) != balance.qty_on_hand:
        raise FifoError("fifo_layer_balance_mismatch")
    balance.qty_quarantined = Decimal(result["quarantined_qty"])
    if balance.qty_reserved < ZERO or balance.qty_reserved + balance.qty_quarantined > balance.qty_on_hand:
        raise FifoError("fifo_reservation_conflict")
    balance.avg_cost = Decimal(result["avg_cost"]) if result["avg_cost"] is not None else None
    balance.total_value = Decimal(result["total_value"]) if result["total_value"] is not None else None
    await db.flush()
    return result


async def receipt_valuation(db, balance, quantity, unit_cost, cost_status="known", stock_status="available"):
    """Stage pristine FIFO activation and compute immutable movement values before INSERT."""
    state = await ensure_fifo(db, balance, "receipt")
    if state is None:
        return None
    from types import SimpleNamespace
    layers = await _layers(db, balance, lock=True)
    projected = [SimpleNamespace(quantity_remaining=row.quantity_remaining, unit_cost=row.unit_cost,
        cost_status=row.cost_status, stock_status=row.stock_status) for row in layers]
    projected.append(SimpleNamespace(quantity_remaining=quantity, unit_cost=unit_cost,
                                     cost_status=cost_status, stock_status=stock_status))
    return summarize(projected, state.revision + 1)


async def add_receipt(db, balance, movement, physical_received_at, unit_cost, cost_status, provenance,
                      stock_status="available", root_cost_layer_id=None):
    """Balance already includes this positive movement; never synthesize old layers."""
    if movement.quantity <= ZERO or movement.quantity != movement.quantity.quantize(QTY):
        raise FifoError("fifo_quantity_invalid")
    state = await ensure_fifo(db, balance, "receipt", previous_qty=balance.qty_on_hand - movement.quantity,
                              excluded_movement_id=movement.id)
    if state is None:
        if unit_cost is None:
            raise FifoError("fifo_cutover_required")
        return None
    layer = FifoLayer(product_id=balance.product_id, warehouse_id=balance.warehouse_id, receipt_movement_id=movement.id,
        physical_received_at=physical_received_at, quantity_original=movement.quantity, quantity_remaining=movement.quantity,
        unit_cost=unit_cost, cost_status=cost_status, stock_status=stock_status, provenance=provenance,
        root_cost_layer_id=root_cost_layer_id, cost_revision=0)
    db.add(layer)
    await db.flush()
    if layer.root_cost_layer_id is None:
        layer.root_cost_layer_id = layer.id
    state.revision += 1
    await revalue(db, balance, state)
    if movement.avg_cost_after != balance.avg_cost:
        raise FifoError("fifo_receipt_valuation_mismatch")
    return layer


async def plan_issue(db, balance, lines, lock=False):
    """Immutable plan for ordered line keys. Missing mode means legacy accounting."""
    current = await valuation(db, balance, lock)
    if current["mode"] != "fifo":
        return None
    layers = sorted(await _layers(db, balance, lock), key=lambda layer: (layer.physical_received_at, layer.id))
    remaining = {layer.id: Decimal(layer.quantity_remaining) for layer in layers}
    allocations = []
    unknown_qty = provisional_qty = known_cost = provisional_cost = ZERO
    for line in sorted(lines, key=lambda row: row["line_key"]):
        need = Decimal(line["quantity"])
        for layer in layers:
            if layer.stock_status != "available" or remaining[layer.id] == ZERO:
                continue
            quantity = min(need, remaining[layer.id])
            if quantity == ZERO:
                break
            cost = allocation_cost(remaining[layer.id], quantity, layer.unit_cost)
            allocations.append({"line_key": line["line_key"], "layer_id": layer.id,
                "quantity": number(quantity), "quantity_before": number(remaining[layer.id]),
                "unit_cost": number(layer.unit_cost), "total_cost": number(cost), "cost_status": layer.cost_status,
                "root_cost_layer_id": layer.root_cost_layer_id, "cost_revision": layer.cost_revision})
            if layer.cost_status == "unknown":
                unknown_qty += quantity
            elif layer.cost_status == "provisional":
                provisional_qty += quantity
                provisional_cost += cost
            else:
                known_cost += cost
            remaining[layer.id] -= quantity
            need -= quantity
            if need == ZERO:
                break
        if need:
            raise FifoError("fifo_insufficient_layers")
    # Use detached scalar copies: planning never changes an ORM layer.
    from types import SimpleNamespace
    after = summarize([SimpleNamespace(quantity_remaining=remaining[layer.id], unit_cost=layer.unit_cost,
        cost_status=layer.cost_status, stock_status=layer.stock_status) for layer in layers], current["revision"])
    return {"revision": current["revision"], "allocations": allocations,
            "total_cost": number(known_cost + provisional_cost) if unknown_qty == ZERO else None,
            "known_cost": number(known_cost), "provisional_cost": number(provisional_cost),
            "unknown_qty": number(unknown_qty), "provisional_qty": number(provisional_qty),
            "value_complete": unknown_qty == ZERO and provisional_qty == ZERO, "after": after}


async def apply_issue(db, balance, movement, planned):
    """Consume this line's frozen allocations and insert its movement only after final valuation."""
    state = await lock_state(db, balance)
    if state is None:
        raise FifoError("fifo_cutover_required")
    layers = {row.id: row for row in await _layers(db, balance, lock=True)}
    quantity = total = ZERO
    unknown = False
    new_allocations = []
    for sequence, row in enumerate(planned):
        layer = layers.get(row["layer_id"])
        amount = Decimal(row["quantity"])
        if (layer is None or layer.stock_status != "available" or layer.quantity_remaining != Decimal(row["quantity_before"])
                or number(layer.unit_cost) != row["unit_cost"] or layer.cost_status != row["cost_status"]
                or layer.cost_revision != row["cost_revision"] or layer.quantity_remaining < amount):
            raise FifoError("fifo_plan_changed")
        cost = allocation_cost(layer.quantity_remaining, amount, layer.unit_cost)
        if number(cost) != row["total_cost"]:
            raise FifoError("fifo_plan_changed")
        layer.quantity_remaining -= amount
        quantity += amount
        unknown = unknown or cost is None
        total += cost if cost is not None else ZERO
        new_allocations.append(FifoAllocation(layer_id=layer.id, sequence=sequence,
            quantity=amount, quantity_before=Decimal(row["quantity_before"]), returned_quantity=ZERO,
            unit_cost_at_issue=layer.unit_cost, total_cost_at_issue=cost, cost_status_at_issue=layer.cost_status,
            unit_cost_current=layer.unit_cost, total_cost_current=cost, cost_status_current=layer.cost_status))
    if quantity != -movement.quantity:
        raise FifoError("fifo_quantity_invalid")
    balance.qty_on_hand -= quantity
    state.revision += 1
    await revalue(db, balance, state)
    movement.total_cost = None if unknown else total
    movement.unit_cost = None if unknown else money(total / quantity)
    movement.avg_cost_after = balance.avg_cost
    movement.balance_after = balance.qty_on_hand
    db.add(movement)
    await db.flush()
    for allocation in new_allocations:
        allocation.issue_movement_id = movement.id
        db.add(allocation)
    await db.flush()
    return movement.total_cost


async def _product_scope(db, product_id=None, sku=None, warehouse_code=None, lock=False):
    warehouse_query = select(Warehouse).where(Warehouse.code == warehouse_code, Warehouse.is_active.is_(True))
    if lock:
        warehouse_query = warehouse_query.with_for_update(read=True)
    warehouse = await db.scalar(warehouse_query.execution_options(populate_existing=True))
    if warehouse is None:
        raise FifoError("fifo_warehouse_not_found", 404)
    product_query = select(Product).where(Product.id == product_id if product_id is not None else Product.sku == sku)
    if lock:
        product_query = product_query.with_for_update(read=True)
    product = await db.scalar(product_query.execution_options(populate_existing=True))
    if product is None or not product.is_active:
        raise FifoError("fifo_product_not_found", 404)
    collision = await db.scalar(select(Product.id).where(func.lower(Product.sku) == product.sku.lower(), Product.id != product.id).limit(1))
    if collision is not None:
        raise FifoError("fifo_product_ambiguous")
    if lock:
        try:
            balances, _ = await lock_stock_balances(db, {product.id}, warehouse.id, create_missing=False)
        except StockPublicationHoldError as error:
            raise FifoError(error.code, error.status) from None
        balance = balances.get(product.id)
    else:
        balance = await db.scalar(select(StockBalance).where(StockBalance.product_id == product.id,
            StockBalance.warehouse_id == warehouse.id).execution_options(populate_existing=True))
    return product, warehouse, balance


def _balance(balance):
    if balance is None:
        return None
    return {**{key: number(getattr(balance, key)) for key in
            ("qty_on_hand", "qty_reserved", "qty_quarantined", "qty_available", "avg_cost", "total_value", "last_purchase_price")},
            "last_purchase_at": balance.last_purchase_at.isoformat() if balance.last_purchase_at else None}


async def _snapshot(db, product, warehouse, balance):
    last, count = (await db.execute(select(func.max(StockMovement.id), func.count()).where(
        StockMovement.product_id == product.id, StockMovement.warehouse_id == warehouse.id))).one()
    return {"product_id": product.id, "sku": product.sku, "warehouse_id": warehouse.id,
            "balance": _balance(balance), "last_movement_id": last, "movement_count": count,
            "tracking_confirmed": await stock_tracking.is_confirmed(db, product.id, warehouse.id),
            "valuation": await valuation(db, balance)}


async def options(db):
    rows = (await db.scalars(select(Warehouse).where(Warehouse.is_active.is_(True)).order_by(Warehouse.code))).all()
    return {"warehouses": [{"id": row.id, "code": row.code, "name": row.name} for row in rows]}


def _layer_dto(layer):
    result = {key: getattr(layer, key) for key in ("id", "product_id", "warehouse_id", "receipt_movement_id", "cutover_id",
        "physical_received_at", "cost_status", "stock_status", "cost_revision", "root_cost_layer_id", "provenance", "created_at")}
    result.update({key: number(getattr(layer, key)) for key in ("quantity_original", "quantity_remaining", "unit_cost")})
    return result


async def stock(db, product_id, warehouse_code, limit=50, offset=0):
    product, warehouse, balance = await _product_scope(db, product_id=product_id, warehouse_code=warehouse_code)
    condition = (FifoLayer.product_id == product.id) & (FifoLayer.warehouse_id == warehouse.id) & stock_tracking.current_layer()
    rows = (await db.scalars(select(FifoLayer).where(condition).order_by(FifoLayer.physical_received_at, FifoLayer.id)
                            .limit(limit).offset(offset))).all()
    snapshot = await _snapshot(db, product, warehouse, balance)
    return {"product_id": product.id, "sku": product.sku, "warehouse": {"id": warehouse.id, "code": warehouse.code, "name": warehouse.name},
            "balance": _balance(balance) if snapshot["tracking_confirmed"] else None,
            "tracking_confirmed": snapshot["tracking_confirmed"],
            "valuation": snapshot["valuation"], "snapshot_hash": digest(snapshot),
            "layers": [_layer_dto(row) for row in rows],
            "total": await db.scalar(select(func.count()).select_from(FifoLayer).where(condition)), "limit": limit, "offset": offset}


async def history(db, product_id, warehouse_code, limit=50, offset=0):
    product, warehouse, _ = await _product_scope(db, product_id=product_id, warehouse_code=warehouse_code)
    condition = (StockMovement.product_id == product.id) & (StockMovement.warehouse_id == warehouse.id)
    rows = (await db.scalars(select(StockMovement).where(condition).order_by(StockMovement.created_at.desc(), StockMovement.id.desc())
                            .limit(limit).offset(offset))).all()
    return {"movements": [{"id": row.id, "movement_type": row.movement_type.value, "quantity": number(row.quantity),
                "unit_cost": number(row.unit_cost), "total_cost": number(row.total_cost), "balance_after": number(row.balance_after),
                "avg_cost_after": number(row.avg_cost_after), "reference_type": row.reference_type,
                "reference_id": row.reference_id, "created_at": row.created_at} for row in rows],
            "total": await db.scalar(select(func.count()).select_from(StockMovement).where(condition)), "limit": limit, "offset": offset}


async def allocations(db, movement_id):
    rows = (await db.scalars(select(FifoAllocation).where(FifoAllocation.issue_movement_id == movement_id)
                            .order_by(FifoAllocation.sequence))).all()
    return {"movement_id": movement_id, "allocations": [{**{key: getattr(row, key) for key in
        ("id", "layer_id", "sequence", "cost_status_at_issue", "cost_status_current", "created_at")},
        **{key: number(getattr(row, key)) for key in ("quantity", "quantity_before", "returned_quantity",
        "unit_cost_at_issue", "total_cost_at_issue", "unit_cost_current", "total_cost_current")}} for row in rows]}


def _cutover_dto(batch):
    return {key: getattr(batch, key) for key in ("id", "status", "preview_hash", "preview_data", "created_at", "expires_at", "result")}


async def get_cutover(db, identifier):
    batch = await db.get(FifoCutover, str(identifier))
    if batch is None:
        raise FifoError("fifo_cutover_not_found", 404)
    return _cutover_dto(batch)


async def cutover_preview(db, payload):
    raw = payload.model_dump(mode="json")
    request_hash = digest(raw)
    product, warehouse, balance = await _product_scope(db, sku=payload.sku, warehouse_code=payload.warehouse_code, lock=True)
    existing = await db.get(FifoCutover, str(payload.request_id))
    if existing:
        if existing.request_hash != request_hash:
            raise FifoError("fifo_request_conflict")
        result = _cutover_dto(existing)
        await db.commit()
        return result
    if not await stock_tracking.is_confirmed(db, product.id, warehouse.id):
        raise FifoError("stock_tracking_required")
    if balance is None:
        raise FifoError("fifo_balance_missing")
    if await lock_state(db, balance) is not None:
        raise FifoError("fifo_already_active")
    if balance.qty_on_hand < ZERO or balance.qty_quarantined != ZERO:
        raise FifoError("fifo_balance_invalid")
    at = now()
    if payload.counted_at > at or any(layer.physical_received_at > payload.counted_at for layer in payload.layers):
        raise FifoError("fifo_date_invalid", 422)
    if sum((Decimal(layer.quantity) for layer in payload.layers), ZERO) != balance.qty_on_hand:
        raise FifoError("fifo_cutover_quantity_mismatch")
    from types import SimpleNamespace
    summarize([SimpleNamespace(quantity_remaining=Decimal(row.quantity),
        unit_cost=Decimal(row.unit_cost) if row.unit_cost is not None else None,
        cost_status=row.cost_status, stock_status="available") for row in payload.layers])
    snapshot = await _snapshot(db, product, warehouse, balance)
    data = {**raw, "product_id": product.id, "warehouse_id": warehouse.id, "snapshot": snapshot,
            "snapshot_hash": digest(snapshot), "currency": "EUR", "vat_included": False}
    batch = FifoCutover(id=str(payload.request_id), product_id=product.id, warehouse_id=warehouse.id,
        request_hash=request_hash, preview_hash=digest(data), preview_data=data, status="prepared", created_at=at,
        expires_at=at + timedelta(minutes=30))
    db.add(batch)
    await db.flush()
    result = _cutover_dto(batch)
    await db.commit()
    return result


async def cutover_apply(db, identifier, payload):
    original = await db.get(FifoCutover, str(identifier))
    if original is None:
        raise FifoError("fifo_cutover_not_found", 404)
    if original.preview_hash != payload.preview_hash:
        raise FifoError("fifo_preview_changed")
    if original.status == "completed":
        return original.result
    product, warehouse, balance = await _product_scope(db, product_id=original.product_id,
        warehouse_code=original.preview_data["warehouse_code"], lock=True)
    batch = await db.scalar(select(FifoCutover).where(FifoCutover.id == original.id).with_for_update()
                            .execution_options(populate_existing=True))
    if batch.status == "completed":
        return batch.result
    if batch.expires_at <= now():
        raise FifoError("fifo_preview_expired")
    if balance is None or await lock_state(db, balance) is not None:
        raise FifoError("fifo_already_active")
    if digest(batch.preview_data) != batch.preview_hash or digest(await _snapshot(db, product, warehouse, balance)) != batch.preview_data["snapshot_hash"]:
        raise FifoError("fifo_preview_stale")
    state = FifoState(product_id=product.id, warehouse_id=warehouse.id, revision=1,
                      activated_at=now(), activation_kind="documented_cutover", cutover_id=batch.id)
    db.add(state)
    layers = []
    for row in batch.preview_data["layers"]:
        layer = FifoLayer(product_id=product.id, warehouse_id=warehouse.id, cutover_id=batch.id,
            physical_received_at=datetime.fromisoformat(row["physical_received_at"].replace("Z", "+00:00")),
            quantity_original=Decimal(row["quantity"]), quantity_remaining=Decimal(row["quantity"]),
            unit_cost=Decimal(row["unit_cost"]) if row["unit_cost"] is not None else None,
            cost_status=row["cost_status"], stock_status="available", cost_revision=0,
            provenance={"source_reference": row["source_reference"], "cutover_source": batch.preview_data["source_reference"],
                        "operator_name": batch.preview_data["operator_name"], "counted_at": batch.preview_data["counted_at"],
                        "unit_cost_at_cutover": row["unit_cost"], "cost_status_at_cutover": row["cost_status"]})
        db.add(layer)
        layers.append(layer)
    await db.flush()
    for layer in layers:
        layer.root_cost_layer_id = layer.id
    result = {"cutover_id": batch.id, "product_id": product.id, "warehouse_id": warehouse.id,
              "layer_ids": [layer.id for layer in layers], "valuation": await revalue(db, balance, state),
              "completed_at": now().isoformat()}
    batch.status, batch.completed_at, batch.result = "completed", now(), result
    await db.flush()
    await db.commit()
    return result


async def get_receipt(db, identifier):
    batch = await db.get(FifoReceipt, str(identifier))
    if batch is None:
        raise FifoError("fifo_receipt_not_found", 404)
    return _cutover_dto(batch)


async def receipt_preview(db, payload):
    raw = payload.model_dump(mode="json")
    request_hash = digest(raw)
    product, warehouse, balance = await _product_scope(db, sku=payload.sku, warehouse_code=payload.warehouse_code, lock=True)
    existing = await db.get(FifoReceipt, str(payload.request_id))
    if existing:
        if existing.request_hash != request_hash:
            raise FifoError("fifo_request_conflict")
        result = _cutover_dto(existing)
        await db.commit()
        return result
    at = now()
    if payload.physical_received_at > at:
        raise FifoError("fifo_date_invalid", 422)
    snapshot = await _snapshot(db, product, warehouse, balance)
    if not snapshot["tracking_confirmed"]:
        await stock_tracking.validate_start(db, balance)
    if snapshot["tracking_confirmed"] and snapshot["valuation"]["mode"] != "fifo" and (snapshot["movement_count"] or balance and balance.qty_on_hand != ZERO):
        raise FifoError("fifo_cutover_required")
    from types import SimpleNamespace
    layers = await _layers(db, balance) if balance else []
    summarize([*layers, SimpleNamespace(quantity_remaining=Decimal(payload.quantity),
        unit_cost=Decimal(payload.unit_cost) if payload.unit_cost is not None else None,
        cost_status=payload.cost_status, stock_status="available")])
    data = {**raw, "product_id": product.id, "warehouse_id": warehouse.id, "snapshot_hash": digest(snapshot),
            "snapshot": snapshot, "currency": "EUR", "vat_included": False}
    batch = FifoReceipt(id=str(payload.request_id), product_id=product.id, warehouse_id=warehouse.id,
        request_hash=request_hash, preview_hash=digest(data), preview_data=data, status="prepared", created_at=at,
        expires_at=at + timedelta(minutes=30))
    db.add(batch)
    await db.flush()
    result = _cutover_dto(batch)
    await db.commit()
    return result


async def receipt_apply(db, identifier, payload):
    original = await db.get(FifoReceipt, str(identifier))
    if original is None:
        raise FifoError("fifo_receipt_not_found", 404)
    if original.preview_hash != payload.preview_hash:
        raise FifoError("fifo_preview_changed")
    if original.status == "completed":
        return original.result
    product, warehouse, balance = await _product_scope(db, product_id=original.product_id,
        warehouse_code=original.preview_data["warehouse_code"], lock=True)
    batch = await db.scalar(select(FifoReceipt).where(FifoReceipt.id == original.id).with_for_update()
                            .execution_options(populate_existing=True))
    if batch.status == "completed":
        return batch.result
    if batch.expires_at <= now():
        raise FifoError("fifo_preview_expired")
    if digest(batch.preview_data) != batch.preview_hash or digest(await _snapshot(db, product, warehouse, balance)) != batch.preview_data["snapshot_hash"]:
        raise FifoError("fifo_preview_stale")
    was_missing = balance is None
    try:
        balances, created_ids = await lock_stock_balances(db, {product.id}, warehouse.id)
    except StockPublicationHoldError as error:
        raise FifoError(error.code, error.status) from None
    if was_missing and product.id not in created_ids:
        raise FifoError("fifo_preview_stale")
    balance = balances[product.id]
    data = batch.preview_data
    await stock_tracking.activate(db, balance, source_type="fifo_receipt", source_id=batch.id,
                                 operator_name=data["operator_name"])
    quantity = Decimal(data["quantity"])
    cost = Decimal(data["unit_cost"]) if data["unit_cost"] is not None else None
    projected = await receipt_valuation(db, balance, quantity, cost, data["cost_status"])
    if projected is None:
        raise FifoError("fifo_cutover_required")
    movement = StockMovement(idempotency_key=f"fifo-receipt:{batch.id}", product_id=product.id, warehouse_id=warehouse.id,
        movement_type=MovementType.RECEIVING_IN, quantity=quantity, unit_cost=cost,
        total_cost=money(quantity*cost) if cost is not None else None,
        unit_cost_original=cost, unit_cost_currency="EUR", fx_rate_to_eur=Decimal("1"),
        reference_type="fifo_receipt", reference_id=batch.id, reference_source=data["source_reference"][:100],
        balance_after=balance.qty_on_hand+quantity,
        avg_cost_after=Decimal(projected["avg_cost"]) if projected["avg_cost"] is not None else None,
        created_by=data["operator_name"], created_at=now())
    db.add(movement)
    await db.flush()
    balance.qty_on_hand += quantity
    layer = await add_receipt(db, balance, movement,
        datetime.fromisoformat(data["physical_received_at"].replace("Z", "+00:00")), cost, data["cost_status"],
        {"kind": "documented_receipt", "receipt_id": batch.id, "source_reference": data["source_reference"],
         "operator_name": data["operator_name"], "unit_cost_at_receipt": data["unit_cost"],
         "cost_status_at_receipt": data["cost_status"]})
    balance.last_movement_at, balance.last_movement_id = movement.created_at, movement.id
    if balance.last_purchase_at is None or layer.physical_received_at >= balance.last_purchase_at:
        balance.last_purchase_price, balance.last_purchase_at = cost, layer.physical_received_at
    result = {"receipt_id": batch.id, "movement_id": movement.id, "layer_id": layer.id,
              "product_id": product.id, "warehouse_id": warehouse.id, "quantity": number(quantity),
              "valuation": await valuation(db, balance), "completed_at": now().isoformat()}
    batch.status, batch.completed_at, batch.result = "completed", now(), result
    await db.flush()
    await db.commit()
    return result
