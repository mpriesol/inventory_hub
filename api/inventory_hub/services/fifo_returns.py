"""Physical returns and documented current-cost corrections, without order edits.

An issued allocation is never rewritten. Returns reduce its net consumption and
create a new quarantined physical layer. Current cost reports follow root cost
revisions; historical movement and at-issue/at-return snapshots remain intact.
"""
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select, text

from inventory_hub.db_models import MovementType, Warehouse
from inventory_hub.db_models_ext import StockBalance, StockMovement
from inventory_hub.fifo_models import FifoAllocation, FifoLayer
from inventory_hub.fifo_return_models import FifoCostRevision, FifoRelease, FifoReturn, FifoReturnLine
from inventory_hub.services import fifo
from inventory_hub.services.stock_balances import lock_stock_balances
from inventory_hub.services.stock_publication_gate import require_stock_write_allowed


ZERO = Decimal("0")
FOUR = Decimal("0.0001")
MAX_QTY = Decimal("999999999.999")
MAX_VALUE = Decimal("999999999999.9999")


class FifoReturnError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def now():
    return datetime.now(timezone.utc)


def number(value):
    return None if value is None else format(value, "f")


def rounded(value):
    return value.quantize(FOUR, rounding=ROUND_HALF_UP)


def consumed_cost(quantity_before, quantity, unit_cost):
    """Cost of the first `quantity` units consumed from an original layer slice."""
    if quantity == 0:
        return ZERO
    if unit_cost is None:
        return None
    return rounded(quantity_before * unit_cost) - rounded((quantity_before - quantity) * unit_cost)


def consumption_summary(allocations):
    """Current net expense: returns reverse the latest units of each allocation."""
    known = provisional = quantity = unknown_qty = provisional_qty = ZERO
    for allocation in allocations:
        net = allocation.quantity - allocation.returned_quantity
        if net < 0:
            raise FifoReturnError("fifo_return_allocation_invalid")
        if not net:
            continue
        quantity += net
        cost = consumed_cost(allocation.quantity_before, net, allocation.unit_cost_current)
        if allocation.cost_status_current == "unknown" or cost is None:
            unknown_qty += net
        elif allocation.cost_status_current == "provisional":
            provisional_qty += net
            provisional += cost
        else:
            known += cost
    return {"quantity": number(quantity), "known_cost": number(known),
            "provisional_cost": number(provisional), "unknown_qty": number(unknown_qty),
            "provisional_qty": number(provisional_qty),
            "total_cost": None if unknown_qty else number(known + provisional),
            "value_complete": not unknown_qty and not provisional_qty, "currency": "EUR"}


def plan_return(allocations, quantity):
    """Reverse the latest consumed allocation first, allowing repeated partial returns."""
    remaining = quantity
    result = []
    for allocation in sorted(allocations, key=lambda row: (row.sequence, row.id), reverse=True):
        available = allocation.quantity - allocation.returned_quantity
        if available < 0:
            raise FifoReturnError("fifo_return_allocation_invalid")
        take = min(available, remaining)
        if take > 0:
            if take != take.to_integral_value():
                # Whole order quantities can span fractional receipt layers.
                # A separate fractional-unit return contract is required to
                # preserve residual valuation and releasable quarantine units.
                raise FifoReturnError("fifo_return_fractional_allocation_unsupported")
            result.append((allocation, take))
            remaining -= take
        if not remaining:
            return result
    raise FifoReturnError("fifo_return_quantity_exceeds_issue")


def _digest(payload):
    return hashlib.sha256(json.dumps(payload.model_dump(mode="json"), sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


async def _existing(db, model, operation, payload):
    # Serialize same-request retries before any stock lock. The unique index also
    # protects persistence; no incomplete audit row commits independently.
    lock = int.from_bytes(hashlib.sha256(f"fifo:{operation}:{payload.request_id}".encode()).digest()[:8],
                          "big", signed=True)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
    row = await db.scalar(select(model).where(model.request_id == str(payload.request_id)))
    if row is not None and row.request_hash != _digest(payload):
        raise FifoReturnError("fifo_request_id_conflict")
    return row


async def _balances(db, product_id, warehouse_ids, *, create_ids=frozenset(), active=False):
    ids = sorted(set(warehouse_ids))
    # All warehouse admission locks precede every balance lock, including a
    # transfer's destination. Publication holds cannot appear after admission.
    for warehouse_id in ids:
        await require_stock_write_allowed(db, warehouse_id)
        warehouse = await db.get(Warehouse, warehouse_id)
        if warehouse is None or (active and not warehouse.is_active):
            raise FifoReturnError("fifo_warehouse_unavailable")
    balances = {}
    for warehouse_id in ids:
        rows, _ = await lock_stock_balances(db, {product_id}, warehouse_id,
                                          create_missing=warehouse_id in create_ids)
        if product_id not in rows:
            raise FifoReturnError("fifo_balance_missing")
        balances[warehouse_id] = rows[product_id]
    return balances


async def _states(db, balances):
    states = {}
    for warehouse_id, balance in sorted(balances.items()):
        state = await fifo.lock_state(db, balance)
        if state is None:
            raise FifoReturnError("fifo_activation_required")
        states[warehouse_id] = state
    return states


async def _issue(db, movement_id):
    movement = await db.get(StockMovement, movement_id)
    if movement is None:
        raise FifoReturnError("fifo_issue_not_found", 404)
    if movement.movement_type != MovementType.SALE_OUT or movement.quantity >= 0:
        raise FifoReturnError("fifo_return_requires_sale_issue")
    if movement.quantity != movement.quantity.to_integral_value():
        raise FifoReturnError("fifo_return_requires_whole_pieces")
    if await db.scalar(select(FifoAllocation.id).where(FifoAllocation.issue_movement_id == movement.id).limit(1)) is None:
        raise FifoReturnError("fifo_return_legacy_issue_unsupported")
    return movement


def _valid_allocations(movement, allocations):
    if not allocations:
        raise FifoReturnError("fifo_return_legacy_issue_unsupported")
    if sum((row.quantity for row in allocations), ZERO) != -movement.quantity:
        raise FifoReturnError("fifo_return_allocation_invalid")
    if any(row.quantity <= 0 or row.returned_quantity < 0 or row.returned_quantity > row.quantity
           for row in allocations):
        raise FifoReturnError("fifo_return_allocation_invalid")


async def return_options(db, movement_id):
    movement = await _issue(db, movement_id)
    allocations = list((await db.scalars(select(FifoAllocation).where(
        FifoAllocation.issue_movement_id == movement_id).order_by(FifoAllocation.sequence))).all())
    _valid_allocations(movement, allocations)
    returned = sum((row.returned_quantity for row in allocations), ZERO)
    return {"issue_movement_id": movement.id, "product_id": movement.product_id,
            "warehouse_id": movement.warehouse_id, "issued_quantity": number(-movement.quantity),
            "returned_quantity": number(returned), "returnable_quantity": number(-movement.quantity - returned),
            "allocations": [_allocation_dto(row) for row in allocations],
            "net_consumption": consumption_summary(allocations), "return_stock_status": "quarantine"}


def _allocation_dto(row):
    net = row.quantity - row.returned_quantity
    return {"id": row.id, "issue_movement_id": row.issue_movement_id, "layer_id": row.layer_id,
            "sequence": row.sequence, "quantity": number(row.quantity),
            "returned_quantity": number(row.returned_quantity), "net_quantity": number(net),
            "unit_cost_at_issue": number(row.unit_cost_at_issue),
            "total_cost_at_issue": number(row.total_cost_at_issue), "cost_status_at_issue": row.cost_status_at_issue,
            "unit_cost_current": number(row.unit_cost_current),
            "total_cost_current": number(row.total_cost_current), "cost_status_current": row.cost_status_current,
            "net_cost_current": number(consumed_cost(row.quantity_before, net, row.unit_cost_current))}


def _new_movement(balance, *, key, movement_type, quantity, cost, reference_type, reference_id, notes, stamp):
    value = None if cost is None else rounded(abs(quantity) * cost)
    if value is not None and value > MAX_VALUE:
        raise FifoReturnError("fifo_value_out_of_range")
    return StockMovement(idempotency_key=key, product_id=balance.product_id, warehouse_id=balance.warehouse_id,
        movement_type=movement_type, quantity=quantity, unit_cost=cost,
        total_cost=value,
        unit_cost_original=cost, unit_cost_currency="EUR", fx_rate_to_eur=Decimal("1"),
        reference_type=reference_type, reference_id=reference_id, reference_source="operator",
        balance_after=balance.qty_on_hand, avg_cost_after=balance.avg_cost,
        notes=notes, created_by="operator", created_at=stamp)


def _record_balance(balance, movement, stamp):
    balance.last_movement_id = movement.id
    balance.last_movement_at = stamp


async def receive_return(db, payload):
    previous = await _existing(db, FifoReturn, "return", payload)
    if previous is not None:
        await db.commit()
        return previous.result
    issue = await _issue(db, payload.issue_movement_id)
    balances = await _balances(db, issue.product_id, [issue.warehouse_id], active=True)
    states = await _states(db, balances)
    balance, state = balances[issue.warehouse_id], states[issue.warehouse_id]
    await fifo.revalue(db, balance, state)
    source_layers = list((await db.scalars(select(FifoLayer).where(FifoLayer.id.in_(
        select(FifoAllocation.layer_id).where(FifoAllocation.issue_movement_id == issue.id)))
        .order_by(FifoLayer.id).with_for_update().execution_options(populate_existing=True))).all())
    layers = {row.id: row for row in source_layers}
    allocations = list((await db.scalars(select(FifoAllocation).where(
        FifoAllocation.issue_movement_id == issue.id).order_by(FifoAllocation.id)
        .with_for_update().execution_options(populate_existing=True))).all())
    _valid_allocations(issue, allocations)
    plan = plan_return(allocations, payload.quantity)
    if balance.qty_on_hand + payload.quantity > MAX_QTY:
        raise FifoReturnError("fifo_quantity_out_of_range")
    stamp = now()
    audit = FifoReturn(request_id=str(payload.request_id), request_hash=_digest(payload),
        issue_movement_id=issue.id, quantity=payload.quantity, case_reference=payload.case_reference,
        reason=payload.reason, condition=payload.condition, received_at=stamp, created_by="operator", result={})
    db.add(audit)
    await db.flush()
    lines = []
    for allocation, quantity in plan:
        source = layers.get(allocation.layer_id)
        if (source is None or source.product_id != issue.product_id or source.warehouse_id != issue.warehouse_id
                or source.root_cost_layer_id is None):
            raise FifoReturnError("fifo_return_lineage_invalid")
        balance.qty_on_hand += quantity
        layer = FifoLayer(product_id=issue.product_id, warehouse_id=issue.warehouse_id,
            physical_received_at=stamp, quantity_original=quantity,
            quantity_remaining=quantity, unit_cost=source.unit_cost, cost_status=source.cost_status,
            stock_status="quarantine", root_cost_layer_id=source.root_cost_layer_id, cost_revision=0,
            provenance={"kind": "physical_return", "return_id": audit.id, "allocation_id": allocation.id,
                        "source_layer_id": source.id, "case_reference": payload.case_reference,
                        "condition": payload.condition, "physical_received": True})
        db.add(layer)
        allocation.returned_quantity += quantity
        await db.flush()
        await fifo.revalue(db, balance, state)
        # The database forbids even an in-transaction UPDATE of a movement.
        # Establish the final layer valuation before the first INSERT; attach
        # the nullable receipt FK only after the immutable movement has its ID.
        movement = _new_movement(balance, key=f"fifo-return:{payload.request_id}:{allocation.id}",
            movement_type=MovementType.RETURN_IN, quantity=quantity, cost=source.unit_cost,
            reference_type="fifo_return", reference_id=str(payload.request_id), notes=payload.reason, stamp=stamp)
        db.add(movement)
        await db.flush()
        layer.receipt_movement_id = movement.id
        _record_balance(balance, movement, stamp)
        db.add(FifoReturnLine(return_id=audit.id, allocation_id=allocation.id, layer_id=layer.id,
            movement_id=movement.id, quantity=quantity, unit_cost_at_return=source.unit_cost,
            cost_status_at_return=source.cost_status))
        lines.append({"allocation_id": allocation.id, "layer_id": layer.id, "movement_id": movement.id,
                      "root_layer_id": source.root_cost_layer_id, "quantity": number(quantity),
                      "unit_cost_at_return": number(source.unit_cost), "cost_status_at_return": source.cost_status})
    state.revision += 1
    valuation = await fifo.revalue(db, balance, state)
    audit.result = {"id": audit.id, "request_id": str(payload.request_id), "issue_movement_id": issue.id,
        "quantity": number(payload.quantity), "condition": payload.condition, "stock_status": "quarantine",
        "received_at": stamp.isoformat(), "lines": lines, "valuation": valuation,
        "net_consumption": consumption_summary(allocations)}
    await db.commit()
    return audit.result


async def release_quarantine(db, payload):
    previous = await _existing(db, FifoRelease, "release", payload)
    if previous is not None:
        await db.commit()
        return previous.result
    initial = await db.get(FifoLayer, payload.source_layer_id)
    if initial is None:
        raise FifoReturnError("fifo_layer_not_found", 404)
    balances = await _balances(db, initial.product_id, [initial.warehouse_id, payload.target_warehouse_id],
                              create_ids={payload.target_warehouse_id}, active=True)
    states = {}
    for warehouse_id, balance in sorted(balances.items()):
        state = await fifo.ensure_fifo(db, balance, activation_kind="quarantine_release")
        if state is None:
            raise FifoReturnError("fifo_activation_required")
        states[warehouse_id] = state
        await fifo.revalue(db, balance, state)
    source = await db.scalar(select(FifoLayer).where(FifoLayer.id == initial.id).with_for_update()
                             .execution_options(populate_existing=True))
    if source.stock_status != "quarantine":
        raise FifoReturnError("fifo_release_requires_quarantine")
    if source.provenance.get("kind") != "physical_return" or source.provenance.get("condition") != "good":
        raise FifoReturnError("fifo_release_condition_not_good")
    if source.root_cost_layer_id is None:
        raise FifoReturnError("fifo_return_lineage_invalid")
    if payload.quantity > source.quantity_remaining:
        raise FifoReturnError("fifo_release_quantity_exceeded")
    source_balance, target_balance = balances[source.warehouse_id], balances[payload.target_warehouse_id]
    if target_balance is not source_balance and target_balance.qty_on_hand + payload.quantity > MAX_QTY:
        raise FifoReturnError("fifo_quantity_out_of_range")
    stamp = now()
    audit = FifoRelease(request_id=str(payload.request_id), request_hash=_digest(payload),
        source_layer_id=source.id, target_warehouse_id=payload.target_warehouse_id,
        quantity=payload.quantity, reason=payload.reason, created_by="operator", result={})
    db.add(audit)
    await db.flush()
    source.quantity_remaining -= payload.quantity
    source_balance.qty_on_hand -= payload.quantity
    await fifo.revalue(db, source_balance, states[source.warehouse_id])
    out = _new_movement(source_balance, key=f"fifo-release:{payload.request_id}:out",
        movement_type=MovementType.TRANSFER_OUT, quantity=-payload.quantity, cost=source.unit_cost,
        reference_type="fifo_release", reference_id=str(payload.request_id), notes=payload.reason, stamp=stamp)
    db.add(out)
    await db.flush()
    _record_balance(source_balance, out, stamp)
    target_balance.qty_on_hand += payload.quantity
    layer = FifoLayer(product_id=source.product_id, warehouse_id=payload.target_warehouse_id,
        physical_received_at=source.physical_received_at,
        quantity_original=payload.quantity, quantity_remaining=payload.quantity,
        unit_cost=source.unit_cost, cost_status=source.cost_status, stock_status="available",
        root_cost_layer_id=source.root_cost_layer_id, cost_revision=0,
        provenance={"kind": "quarantine_release", "release_id": audit.id, "source_layer_id": source.id,
                    "condition_verified": True, "original_provenance": source.provenance})
    db.add(layer)
    await db.flush()
    valuations = {}
    for warehouse_id, balance in sorted(balances.items()):
        states[warehouse_id].revision += 1
        valuations[str(warehouse_id)] = await fifo.revalue(db, balance, states[warehouse_id])
    incoming = _new_movement(target_balance, key=f"fifo-release:{payload.request_id}:in",
        movement_type=MovementType.TRANSFER_IN, quantity=payload.quantity, cost=source.unit_cost,
        reference_type="fifo_release", reference_id=str(payload.request_id), notes=payload.reason, stamp=stamp)
    db.add(incoming)
    await db.flush()
    layer.receipt_movement_id = incoming.id
    _record_balance(target_balance, incoming, stamp)
    audit.result = {"id": audit.id, "request_id": str(payload.request_id), "source_layer_id": source.id,
        "layer_id": layer.id, "root_layer_id": source.root_cost_layer_id,
        "target_warehouse_id": payload.target_warehouse_id, "quantity": number(payload.quantity),
        "out_movement_id": out.id, "in_movement_id": incoming.id,
        "physical_received_at": layer.physical_received_at.isoformat(), "stock_status": "available",
        "valuations": valuations}
    await db.commit()
    return audit.result


async def _root(db, root_id):
    root = await db.get(FifoLayer, root_id)
    if root is None:
        raise FifoReturnError("fifo_layer_not_found", 404)
    if root.root_cost_layer_id != root.id:
        raise FifoReturnError("fifo_cost_requires_root_layer")
    return root


async def cost_options(db, root_layer_id):
    root = await _root(db, root_layer_id)
    layers = list((await db.scalars(select(FifoLayer).where(
        FifoLayer.root_cost_layer_id == root.id).order_by(FifoLayer.id))).all())
    allocations = list((await db.scalars(select(FifoAllocation).where(
        FifoAllocation.layer_id.in_([row.id for row in layers])).order_by(FifoAllocation.id))).all())
    audits = list((await db.scalars(select(FifoCostRevision).where(FifoCostRevision.root_layer_id == root.id)
        .order_by(FifoCostRevision.revision.desc()).limit(100))).all())
    return {"root_layer_id": root.id, "product_id": root.product_id, "revision": root.cost_revision,
        "unit_cost": number(root.unit_cost), "cost_status": root.cost_status, "currency": "EUR",
        "layers": [{"id": row.id, "warehouse_id": row.warehouse_id,
            "quantity_remaining": number(row.quantity_remaining), "stock_status": row.stock_status,
            "unit_cost": number(row.unit_cost), "cost_status": row.cost_status,
            "physical_received_at": row.physical_received_at.isoformat()} for row in layers],
        "allocations": [_allocation_dto(row) for row in allocations],
        "net_consumption": consumption_summary(allocations),
        "revisions": [{"id": row.id, "revision": row.revision, "previous_cost": number(row.previous_cost),
            "previous_status": row.previous_status, "new_cost": number(row.new_cost),
            "new_status": row.new_status, "reason": row.reason, "document_reference": row.document_reference,
            "created_at": row.created_at.isoformat(), "impact": row.impact} for row in audits]}


async def revise_cost(db, payload):
    previous = await _existing(db, FifoCostRevision, "cost", payload)
    if previous is not None:
        await db.commit()
        return previous.impact
    root = await _root(db, payload.root_layer_id)
    scope = set((await db.scalars(select(FifoLayer.warehouse_id).where(
        FifoLayer.root_cost_layer_id == root.id))).all())
    balances = await _balances(db, root.product_id, scope)
    states = await _states(db, balances)
    # Every lineage creator first locks its existing source balance. After all
    # known source balances are held, no more descendants can appear. If a
    # transfer widened the scope before these locks, require a fresh retry.
    layers = list((await db.scalars(select(FifoLayer).where(FifoLayer.root_cost_layer_id == root.id)
        .order_by(FifoLayer.id).with_for_update().execution_options(populate_existing=True))).all())
    if any(row.warehouse_id not in scope or row.product_id != root.product_id for row in layers):
        raise FifoReturnError("fifo_cost_scope_changed_retry")
    root = next((row for row in layers if row.id == payload.root_layer_id), None)
    if root is None or root.root_cost_layer_id != root.id:
        raise FifoReturnError("fifo_return_lineage_invalid")
    if root.cost_revision != payload.expected_revision:
        raise FifoReturnError("fifo_cost_revision_conflict")
    allocations = list((await db.scalars(select(FifoAllocation).where(
        FifoAllocation.layer_id.in_([row.id for row in layers])).order_by(FifoAllocation.id)
        .with_for_update().execution_options(populate_existing=True))).all())
    before = {str(wid): await fifo.revalue(db, balance, states[wid]) for wid, balance in sorted(balances.items())}
    net_before = consumption_summary(allocations)
    previous_cost, previous_status = root.unit_cost, root.cost_status
    for layer in layers:
        layer.unit_cost, layer.cost_status = payload.new_unit_cost, payload.cost_status
    for allocation in allocations:
        value = consumed_cost(allocation.quantity_before, allocation.quantity, payload.new_unit_cost)
        if value is not None and value > MAX_VALUE:
            raise FifoReturnError("fifo_value_out_of_range")
        allocation.unit_cost_current = payload.new_unit_cost
        allocation.cost_status_current = payload.cost_status
        allocation.total_cost_current = value
    # Correct the display cache only for the latest actual purchase in its
    # original warehouse. Returns, transfers, cutovers and older backdated
    # invoices must not masquerade as a new purchase.
    cached_purchase_updated = False
    if root.provenance.get("kind") in ("receiving", "documented_receipt"):
        latest = await db.scalar(select(FifoLayer.id).where(
            FifoLayer.product_id == root.product_id, FifoLayer.warehouse_id == root.warehouse_id,
            FifoLayer.root_cost_layer_id == FifoLayer.id,
            FifoLayer.provenance["kind"].astext.in_(["receiving", "documented_receipt"]))
            .order_by(FifoLayer.physical_received_at.desc(), FifoLayer.id.desc()).limit(1))
        purchase_balance = balances[root.warehouse_id]
        if latest == root.id and (purchase_balance.last_purchase_at is None
                                 or purchase_balance.last_purchase_at <= root.physical_received_at):
            purchase_balance.last_purchase_price = payload.new_unit_cost
            cached_purchase_updated = True
    root.cost_revision += 1
    after = {}
    for wid, balance in sorted(balances.items()):
        states[wid].revision += 1
        after[str(wid)] = await fifo.revalue(db, balance, states[wid])
    impact = {"request_id": str(payload.request_id), "root_layer_id": root.id, "revision": root.cost_revision,
        "previous_unit_cost": number(previous_cost), "previous_cost_status": previous_status,
        "unit_cost": number(payload.new_unit_cost), "cost_status": payload.cost_status,
        "layer_count": len(layers), "allocation_count": len(allocations),
        "stock_before": before, "stock_after": after,
        "net_consumption_before": net_before, "net_consumption_after": consumption_summary(allocations),
        "currency": "EUR", "historical_snapshots_changed": False,
        "last_purchase_price_updated": cached_purchase_updated}
    audit = FifoCostRevision(request_id=str(payload.request_id), request_hash=_digest(payload),
        root_layer_id=root.id, revision=root.cost_revision, previous_cost=previous_cost,
        previous_status=previous_status, new_cost=payload.new_unit_cost, new_status=payload.cost_status,
        reason=payload.reason, document_reference=payload.document_reference, created_by="operator", impact=impact)
    db.add(audit)
    await db.flush()
    impact = {**impact, "id": audit.id}
    audit.impact = impact
    await db.commit()
    return impact
