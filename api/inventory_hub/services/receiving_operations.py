"""Receipt recovery through exact compensating movements and reversible archival.

No action deletes a receipt, scan, FIFO layer, invoice, or stock movement.
Reversal consumes only this receipt's intact layers, never replacement stock.
"""
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from inventory_hub.db_models import MovementType, Product, ReceivingStatus, Supplier
from inventory_hub.db_models_ext import ReceivingSession, StockMovement
from inventory_hub.fifo_models import FifoAllocation, FifoLayer
from inventory_hub.receiving_operation_models import ReceivingOperation
from inventory_hub.services import fifo, receiving_source, stock_tracking
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK
from inventory_hub.services.stock_balances import lock_stock_balances
from inventory_hub.services.stock_publication_gate import StockPublicationHoldError


class ReceivingOperationError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def posting_revision(session):
    value = (getattr(session, "session_data", None) or {}).get("posting_revision", 0)
    if type(value) is not int or value < 0:
        raise ReceivingOperationError("receiving_revision_invalid")
    return value


def movement_key(session, line_id):
    revision = posting_revision(session)
    return f"receiving:{session.id}:{line_id}" + (f":r{revision}" if revision else "")


def archived(session):
    return bool((getattr(session, "session_data", None) or {}).get("receiving_archive"))


async def assert_invoice_allowed(db, session):
    from inventory_hub.services.invoice_processing import assert_receiving_allowed, InvoiceProcessingError
    try:
        await assert_receiving_allowed(db, session.supplier_id, session.invoice_number, session.id)
    except InvoiceProcessingError as error:
        raise ReceivingOperationError(error.code, error.status) from None


async def _request_lock(db, identifier):
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                     {"key": int(fifo.digest(["receiving-operation", str(identifier)])[:15], 16)})


async def _receipt(db, supplier_code, session_id, *, lock=False):
    supplier = await db.scalar(select(Supplier).where(Supplier.code == supplier_code))
    if supplier is None:
        raise ReceivingOperationError("receiving_session_not_found", 404)
    if lock:
        await receiving_source.lock_supplier(db, supplier.id)
    query = select(ReceivingSession).where(ReceivingSession.id == session_id,
        ReceivingSession.supplier_id == supplier.id).options(
            selectinload(ReceivingSession.lines), selectinload(ReceivingSession.warehouse))
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    session = await db.scalar(query)
    if session is None:
        raise ReceivingOperationError("receiving_session_not_found", 404)
    return session


def _stamp(value):
    return value.isoformat() if value else None


def _session_snapshot(session):
    return {"id": session.id, "supplier_id": session.supplier_id, "warehouse_id": session.warehouse_id,
        "invoice_number": session.invoice_number, "status": session.status.value,
        "source_hash": session.source_hash, "session_data": session.session_data or {},
        "started_at": _stamp(session.started_at), "paused_at": _stamp(session.paused_at),
        "finished_at": _stamp(session.finished_at),
        "lines": [{"id": line.id, "product_id": line.product_id, "line_number": line.line_number,
            "status": line.status, "supplier_sku": line.supplier_sku, "ean": line.ean,
            "ordered_qty": fifo.number(line.ordered_qty), "received_qty": fifo.number(line.received_qty),
            "unit_price": fifo.number(line.unit_price), "purchase_unit": line.purchase_unit,
            "conversion_stock_unit": line.conversion_stock_unit,
            "purchase_to_stock_factor": fifo.number(line.purchase_to_stock_factor)}
            for line in sorted(session.lines, key=lambda row: row.id)]}


async def _plan(db, session, action):
    snapshot = {"session": _session_snapshot(session), "stock": [], "receipt_layers": []}
    view = {"invoice_number": session.invoice_number, "receipt_status": session.status.value,
        "warehouse": {"id": session.warehouse_id, "code": session.warehouse.code, "name": session.warehouse.name},
        "posting_revision": posting_revision(session), "archived": archived(session),
        "stock_effect": "unchanged", "impacts": [], "errors": []}
    context = {"balances": {}, "layers": {}, "movements": []}

    def reject(code, product_id=None):
        view["errors"].append({"code": code, **({"product_id": product_id} if product_id is not None else {})})

    if action == "restore":
        if not archived(session):
            reject("receiving_not_archived")
        else:
            view["after_status"] = session.session_data["receiving_archive"]["previous_status"]
        return snapshot, view, context
    if archived(session):
        reject("receiving_archived")
        return snapshot, view, context
    if action == "archive":
        view["after_status"] = "cancelled"
        return snapshot, view, context
    view["after_status"] = "paused" if action == "reopen" else "cancelled"
    if (action == "reopen" and session.status == ReceivingStatus.cancelled
            and (session.session_data or {}).get("receiving_reversed")):
        # An already compensated receipt may be opened for a new posting cycle;
        # reopening never compensates the same old receipt twice.
        return snapshot, view, context
    if session.status != ReceivingStatus.completed:
        reject("receiving_completed_required")
        return snapshot, view, context

    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": IDENTITY_WRITE_LOCK})
    keys = {movement_key(session, line.id): line for line in session.lines}
    movements = list(await db.scalars(select(StockMovement).where(
        StockMovement.reference_type == "receiving_session", StockMovement.reference_id == str(session.id),
        StockMovement.idempotency_key.in_(keys)).order_by(StockMovement.id)))
    context["movements"] = movements
    seen = {row.idempotency_key for row in movements}
    product_ids = {row.product_id for row in movements}
    products = {row.id: row for row in await db.scalars(select(Product).where(
        Product.id.in_(product_ids | {line.product_id for line in session.lines if line.product_id is not None})))}
    for key, line in keys.items():
        product = products.get(line.product_id)
        if line.received_qty > 0 and (product is None or product.item_type != "service") and key not in seen:
            reject("receiving_reversal_missing_movement", line.product_id)
    current_ids = set(await db.scalars(select(StockMovement.id).where(
        StockMovement.id.in_([row.id for row in movements]), stock_tracking.current_movement())))
    if any(row.id not in current_ids or row.movement_type != MovementType.RECEIVING_IN
           or row.quantity <= 0 or row.warehouse_id != session.warehouse_id for row in movements):
        reject("receiving_reversal_historical")
    try:
        balances, _ = await lock_stock_balances(db, product_ids, session.warehouse_id, create_missing=False)
    except StockPublicationHoldError as error:
        reject(error.code)
        return snapshot, view, context
    context["balances"] = balances
    layers = list(await db.scalars(select(FifoLayer).where(
        FifoLayer.receipt_movement_id.in_([row.id for row in movements])).order_by(FifoLayer.id)
        .with_for_update().execution_options(populate_existing=True)))
    allocated = set(await db.scalars(select(FifoAllocation.layer_id).where(
        FifoAllocation.layer_id.in_([row.id for row in layers]))))
    derived = set(await db.scalars(select(FifoLayer.root_cost_layer_id).where(
        FifoLayer.root_cost_layer_id.in_([row.id for row in layers]), FifoLayer.id != FifoLayer.root_cost_layer_id)))
    for movement in movements:
        selected = [row for row in layers if row.receipt_movement_id == movement.id]
        if len(selected) != 1:
            reject("receiving_reversal_fifo_required", movement.product_id)
            continue
        layer = selected[0]
        context["layers"][movement.id] = layer
        snapshot["receipt_layers"].append({"id": layer.id, "movement_id": movement.id,
            "quantity_original": fifo.number(layer.quantity_original), "quantity_remaining": fifo.number(layer.quantity_remaining),
            "cost": fifo.number(layer.unit_cost), "cost_status": layer.cost_status, "cost_revision": layer.cost_revision,
            "stock_status": layer.stock_status, "provenance": layer.provenance,
            "allocated": layer.id in allocated, "derived": layer.id in derived})
        if (layer.id in allocated or layer.id in derived or layer.quantity_remaining != movement.quantity
                or layer.quantity_original != movement.quantity or layer.stock_status != "available"
                or layer.root_cost_layer_id != layer.id or layer.provenance.get("receiving_reversal_id")):
            reject("receiving_reversal_used_stock", movement.product_id)
    for product_id in sorted(product_ids):
        balance, product = balances.get(product_id), products[product_id]
        if balance is None:
            reject("receiving_reversal_balance_missing", product_id)
            continue
        try:
            stock = await fifo._snapshot(db, product, session.warehouse, balance)
        except fifo.FifoError as error:
            reject(error.code, product_id)
            continue
        snapshot["stock"].append(stock)
        if stock["valuation"]["mode"] != "fifo":
            reject("receiving_reversal_fifo_required", product_id)
        quantity = sum((row.quantity for row in movements if row.product_id == product_id), Decimal(0))
        after = balance.qty_on_hand - quantity
        if after < balance.qty_reserved + balance.qty_quarantined:
            reject("receiving_reversal_committed_stock", product_id)
        view["impacts"].append({"product_id": product_id, "sku": product.sku, "name": product.name,
            "stock_unit": product.stock_unit, "before_quantity": fifo.number(balance.qty_on_hand),
            "delta": fifo.number(-quantity), "after_quantity": fifo.number(after),
            "reserved": fifo.number(balance.qty_reserved), "quarantined": fifo.number(balance.qty_quarantined),
            "available_before": fifo.number(balance.qty_on_hand - balance.qty_reserved - balance.qty_quarantined),
            "available_after": fifo.number(after - balance.qty_reserved - balance.qty_quarantined)})
    if movements:
        view["stock_effect"] = "reverse_receipt"
    return snapshot, view, context


def _dto(row):
    return {"id": row.id, "session_id": row.session_id, "action": row.action, "status": row.status,
        "reason": row.preview_data["reason"], "actor": row.actor, "preview_hash": row.preview_hash,
        "ready": row.status == "prepared", "expires_at": row.expires_at.isoformat(),
        **row.preview_data["view"], "result": row.result}


async def get(db, supplier_code, session_id, identifier):
    await _receipt(db, supplier_code, session_id)
    row = await db.get(ReceivingOperation, str(identifier))
    if row is None or row.session_id != session_id:
        raise ReceivingOperationError("receiving_operation_not_found", 404)
    return _dto(row)


async def history(db, supplier_code, session_id):
    """Completed operation summaries only; never expose frozen receipt snapshots."""
    await _receipt(db, supplier_code, session_id)
    rows = await db.scalars(select(ReceivingOperation).where(
        ReceivingOperation.session_id == session_id, ReceivingOperation.status == "completed")
        .order_by(ReceivingOperation.completed_at.desc(), ReceivingOperation.id.desc()).limit(50))
    return {"session_id": session_id, "limit": 50, "operations": [
        {"id": row.id, "action": row.action, "reason": row.result["reason"], "actor": row.result["actor"],
         "at": row.completed_at.isoformat(), "stock_effect": row.result["stock_effect"],
         "impacts": row.result["impacts"], "status": row.result["status"], "archived": row.result["archived"]}
        for row in rows]}


async def preview(db, supplier_code, session_id, payload, actor):
    await _request_lock(db, payload.request_id)
    session = await _receipt(db, supplier_code, session_id, lock=True)
    request_hash = fifo.digest({"supplier": supplier_code, "session_id": session_id, **payload.model_dump(mode="json")})
    old = await db.get(ReceivingOperation, str(payload.request_id))
    if old:
        if old.request_hash != request_hash:
            raise ReceivingOperationError("receiving_operation_request_conflict")
        result = _dto(old)
        await db.commit()
        return result
    if payload.action in ("reopen", "restore"):
        await assert_invoice_allowed(db, session)
    snapshot, view, _ = await _plan(db, session, payload.action)
    data = {"reason": payload.reason, "snapshot": snapshot, "view": view}
    stamp = fifo.now()
    row = ReceivingOperation(id=str(payload.request_id), session_id=session.id, action=payload.action,
        request_hash=request_hash, preview_hash=fifo.digest(data), preview_data=data, actor=actor,
        status="blocked" if view["errors"] else "prepared", created_at=stamp, expires_at=stamp + timedelta(minutes=30))
    db.add(row)
    await db.flush()
    result = _dto(row)
    await db.commit()
    return result


async def apply(db, supplier_code, session_id, identifier, payload, actor):
    await _request_lock(db, identifier)
    session = await _receipt(db, supplier_code, session_id, lock=True)
    row = await db.get(ReceivingOperation, str(identifier))
    if row is None or row.session_id != session_id:
        raise ReceivingOperationError("receiving_operation_not_found", 404)
    if row.preview_hash != payload.preview_hash:
        raise ReceivingOperationError("receiving_operation_preview_changed")
    if row.status == "completed":
        result = row.result
        await db.commit()
        return result
    if row.status != "prepared":
        raise ReceivingOperationError("receiving_operation_blocked")
    if row.expires_at <= fifo.now():
        raise ReceivingOperationError("receiving_operation_expired")
    if row.action in ("reopen", "restore"):
        await assert_invoice_allowed(db, session)
    snapshot, view, context = await _plan(db, session, row.action)
    if (view["errors"] or fifo.digest(snapshot) != fifo.digest(row.preview_data["snapshot"])
            or fifo.digest(row.preview_data) != row.preview_hash):
        raise ReceivingOperationError("receiving_operation_stale")
    stamp, movement_ids = fifo.now(), []
    reason = row.preview_data["reason"]
    data = dict(session.session_data or {})
    if row.action in ("reverse", "reopen"):
        for original in context["movements"]:
            layer, balance = context["layers"][original.id], context["balances"][original.product_id]
            movement = StockMovement(idempotency_key=f"receiving-reversal:{row.id}:{original.id}",
                product_id=original.product_id, warehouse_id=session.warehouse_id,
                movement_type=MovementType.ADJUSTMENT_OUT, quantity=-original.quantity,
                reference_type="receiving_reversal", reference_id=row.id,
                reference_source=f"{supplier_code}:{session.invoice_number}"[:100],
                notes=reason, created_by=actor, created_at=stamp)
            allocation = {"layer_id": layer.id, "quantity": fifo.number(original.quantity),
                "quantity_before": fifo.number(layer.quantity_remaining), "unit_cost": fifo.number(layer.unit_cost),
                "cost_status": layer.cost_status, "cost_revision": layer.cost_revision,
                "total_cost": fifo.number(fifo.allocation_cost(layer.quantity_remaining, original.quantity, layer.unit_cost))}
            await fifo.apply_issue(db, balance, movement, [allocation])
            layer.provenance = {**layer.provenance, "receiving_reversal_id": row.id}
            balance.last_movement_id, balance.last_movement_at = movement.id, stamp
            movement_ids.append(movement.id)
        await db.flush()
        for balance in context["balances"].values():
            current = await fifo._layers(db, balance)
            latest = next(iter(sorted((layer for layer in current
                if layer.root_cost_layer_id == layer.id and not layer.provenance.get("receiving_reversal_id")
                and layer.provenance.get("kind") in ("receiving", "documented_receipt")),
                key=lambda layer: (layer.physical_received_at, layer.id), reverse=True)), None)
            balance.last_purchase_price = latest.unit_cost if latest else None
            balance.last_purchase_at = latest.physical_received_at if latest else None
        data["posting_revision"] = posting_revision(session) + 1
        data["receiving_reversed"] = {"operation_id": row.id, "at": stamp.isoformat()}
        data.pop("finalize_result", None)
        session.status = ReceivingStatus.paused if row.action == "reopen" else ReceivingStatus.cancelled
        session.finished_at = None if row.action == "reopen" else stamp
        session.paused_at = stamp if row.action == "reopen" else None
    elif row.action == "archive":
        data["receiving_archive"] = {"operation_id": row.id, "archived_at": stamp.isoformat(),
            "reason": reason, "actor": actor, "previous_status": session.status.value,
            "previous_started_at": _stamp(session.started_at), "previous_paused_at": _stamp(session.paused_at),
            "previous_finished_at": _stamp(session.finished_at)}
        session.status = ReceivingStatus.cancelled
    else:
        archive = data.pop("receiving_archive")
        session.status = ReceivingStatus(archive["previous_status"])
        for field in ("started_at", "paused_at", "finished_at"):
            value = archive["previous_" + field]
            setattr(session, field, datetime.fromisoformat(value) if value else None)
    data["receiving_last_operation"] = {"id": row.id, "action": row.action,
        "reason": reason, "actor": actor, "at": stamp.isoformat()}
    session.session_data = data
    result = {"id": row.id, "action": row.action, "reason": reason, "actor": actor,
        "session_id": session.id, "supplier_code": supplier_code, "invoice_number": session.invoice_number,
        "status": session.status.value, "archived": archived(session), "posting_revision": posting_revision(session),
        "stock_effect": view["stock_effect"], "impacts": view["impacts"], "movement_ids": movement_ids,
        "completed_at": stamp.isoformat()}
    row.status, row.completed_at, row.result = "completed", stamp, result
    await db.flush()
    await db.commit()
    return result
