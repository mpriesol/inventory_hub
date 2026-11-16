"""Shared physical receipt writer for invoice and mixed receiving workflows."""
from datetime import datetime, timezone
from decimal import Decimal
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from inventory_hub.db_models import Product, MovementType
from inventory_hub.db_models_ext import ReceivingSession, ReceivingLine, StockBalance, StockMovement
from inventory_hub.services import fifo, stock_tracking, receiving_operations
from inventory_hub.services.item_units import receipt_values, validate_stock_quantity, ItemUnitError


async def write_stock_for_line(
    db: AsyncSession,
    session: ReceivingSession,
    line: ReceivingLine,
    product: Product,
    supplier_code: str,
    balance: StockBalance,
    operator_name: str = "receiving",
    *, purchase_quantity: Decimal | None = None, idempotency_key: str | None = None,
) -> StockMovement:
    """
    Append one confirmed receipt through the shared FIFO/stock tracking engine.
    A batch supplies its exact purchase quantity and immutable operation key.
    """
    purchase_quantity = line.received_qty if purchase_quantity is None else purchase_quantity
    if product.item_type != "stock":
        raise HTTPException(409, detail={"code": "stock_item_required"})
    try:
        qty, unit_cost = receipt_values(purchase_quantity, line.unit_price, line.purchase_to_stock_factor)
        validate_stock_quantity(qty, product.stock_unit)
        if line.conversion_stock_unit is not None and line.conversion_stock_unit != product.stock_unit:
            raise ItemUnitError("receiving_unit_changed")
    except ItemUnitError as error:
        raise HTTPException(409, detail={"code": error.code, "line_id": line.id}) from None

    try:
        await stock_tracking.activate(db, balance, source_type="receiving_session", source_id=session.id,
                                     operator_name=operator_name)
    except fifo.FifoError as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.code}) from None

    old_qty = balance.qty_on_hand or Decimal("0")
    old_avg = balance.avg_cost
    state = await fifo.lock_state(db, balance)
    if state is None and old_avg is None:
        raise HTTPException(409, detail={"code": "fifo_cutover_required", "message": "fifo_cutover_required"})
    new_qty = old_qty + qty
    if new_qty > fifo.MAX_QUANTITY:
        raise HTTPException(409, detail={"code": "fifo_value_out_of_range", "message": "fifo_value_out_of_range"})
    if unit_cost is not None and new_qty > 0 and old_avg is not None:
        new_avg = ((old_qty * old_avg) + (qty * unit_cost)) / new_qty
    else:
        new_avg = old_avg

    try:
        projected = await fifo.receipt_valuation(db, balance, qty, unit_cost)
    except fifo.FifoError as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.code}) from None
    if projected is not None:
        new_avg = Decimal(projected["avg_cost"]) if projected["avg_cost"] is not None else None
    now = datetime.now(timezone.utc)
    movement = StockMovement(
        idempotency_key=idempotency_key or receiving_operations.movement_key(session, line.id),
        product_id=product.id,
        warehouse_id=session.warehouse_id,
        movement_type=MovementType.RECEIVING_IN,
        quantity=qty,
        unit_cost=unit_cost,
        reference_type="receiving_session",
        reference_id=str(session.id),
        reference_source=f"{supplier_code}:{session.invoice_number}",
        balance_after=new_qty,
        avg_cost_after=new_avg,
        created_by=operator_name,
    )
    db.add(movement)
    await db.flush()

    balance.qty_on_hand = new_qty
    balance.avg_cost = new_avg
    balance.total_value = new_qty * new_avg if new_avg is not None else None
    if unit_cost is not None:
        balance.last_purchase_price = unit_cost
        balance.last_purchase_at = now
    balance.last_movement_at = now
    balance.last_movement_id = movement.id
    try:
        await fifo.add_receipt(db, balance, movement, now, unit_cost, "known",
            {"kind": "receiving", "session_id": session.id, "line_id": line.id,
             "supplier_code": supplier_code, "invoice_number": session.invoice_number,
             "purchase_quantity": str(purchase_quantity), "purchase_unit": line.purchase_unit or product.stock_unit,
             "purchase_to_stock_factor": str(line.purchase_to_stock_factor), "stock_unit": product.stock_unit,
             "unit_cost_at_receipt": str(unit_cost)})
    except fifo.FifoError as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.code}) from None
    return movement
