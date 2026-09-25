"""Explicit stock start. Call activate only from confirmed count/receipt writers.

The caller holds the warehouse gate and the product's balance row lock and owns
the transaction. Archival closing movements preserve ledger continuity; old FIFO
layers and issued allocations remain untouched and outside the new inventory.
"""
from sqlalchemy import exists, func, select
from inventory_hub.db_models import MovementType, ReservationStatus
from inventory_hub.db_models_ext import Reservation, StockBalance, StockMovement
from inventory_hub.fifo_models import FifoLayer, FifoState
from inventory_hub.stock_tracking_models import StockTracking


def confirmed_stock(product_id=StockBalance.product_id, warehouse_id=StockBalance.warehouse_id):
    return exists(select(StockTracking.product_id).where(
        StockTracking.product_id == product_id, StockTracking.warehouse_id == warehouse_id))


def current_movement(movement=StockMovement):
    return exists(select(StockTracking.product_id).where(
        StockTracking.product_id == movement.product_id, StockTracking.warehouse_id == movement.warehouse_id,
        movement.id > StockTracking.historical_last_movement_id))


def current_layer(layer=FifoLayer):
    return exists(select(StockTracking.product_id).where(
        StockTracking.product_id == layer.product_id, StockTracking.warehouse_id == layer.warehouse_id,
        layer.id > StockTracking.historical_last_layer_id))


async def get(db, product_id, warehouse_id):
    return await db.scalar(select(StockTracking).where(StockTracking.product_id == product_id,
        StockTracking.warehouse_id == warehouse_id).execution_options(populate_existing=True))


async def is_confirmed(db, product_id, warehouse_id):
    return bool(await db.scalar(select(confirmed_stock(product_id, warehouse_id))))


async def validate_start(db, balance):
    from inventory_hub.services import fifo
    if balance is None:
        return
    if balance.qty_reserved or balance.qty_quarantined or await db.scalar(select(Reservation.id).where(
        Reservation.product_id == balance.product_id, Reservation.warehouse_id == balance.warehouse_id,
        Reservation.status.in_((ReservationStatus.reserved, ReservationStatus.backorder))).limit(1)):
        raise fifo.FifoError("stock_tracking_committed_stock")


async def activate(db, balance, *, source_type, source_id, operator_name):
    from inventory_hub.services import fifo
    previous = await get(db, balance.product_id, balance.warehouse_id)
    if previous is not None:
        return previous
    await validate_start(db, balance)
    stamp = fifo.now()
    snapshot = {**fifo._balance(balance), "last_movement_id": balance.last_movement_id}
    last_movement = int(await db.scalar(select(func.max(StockMovement.id)).where(
        StockMovement.product_id == balance.product_id, StockMovement.warehouse_id == balance.warehouse_id)) or 0)
    last_layer = int(await db.scalar(select(func.max(FifoLayer.id)).where(
        FifoLayer.product_id == balance.product_id, FifoLayer.warehouse_id == balance.warehouse_id)) or 0)
    state = await fifo.lock_state(db, balance)
    if state:
        snapshot["fifo_state"] = {"revision": state.revision, "activation_kind": state.activation_kind,
            "activated_at": state.activated_at.isoformat(), "cutover_id": state.cutover_id}
    if balance.qty_on_hand:
        # This closes historical evidence; it is neither a sale nor a new receipt.
        closing = StockMovement(idempotency_key=f"stock-start:{balance.product_id}:{balance.warehouse_id}",
            product_id=balance.product_id, warehouse_id=balance.warehouse_id,
            movement_type=MovementType.ADJUSTMENT_OUT if balance.qty_on_hand > 0 else MovementType.ADJUSTMENT_IN,
            quantity=-balance.qty_on_hand, unit_cost=balance.avg_cost, total_cost=balance.total_value,
            reference_type="stock_tracking", reference_id=f"{balance.product_id}:{balance.warehouse_id}",
            reference_source="historical_stock_closure", balance_after=fifo.ZERO, avg_cost_after=fifo.ZERO,
            notes="Uzavretie historického neovereného stavu pred začiatkom novej evidencie.",
            created_by=operator_name, created_at=stamp)
        db.add(closing)
        await db.flush()
        last_movement = closing.id
        balance.last_movement_id, balance.last_movement_at = closing.id, stamp
    tracking = StockTracking(product_id=balance.product_id, warehouse_id=balance.warehouse_id,
        started_at=stamp, source_type=source_type, source_id=str(source_id), operator_name=operator_name,
        historical_last_movement_id=last_movement, historical_last_layer_id=last_layer, archived_balance=snapshot)
    db.add(tracking)
    balance.qty_on_hand = balance.qty_reserved = balance.qty_quarantined = fifo.ZERO
    balance.avg_cost = balance.total_value = fifo.ZERO
    balance.last_purchase_price = balance.last_purchase_at = None
    if state is None:
        state = FifoState(product_id=balance.product_id, warehouse_id=balance.warehouse_id,
            revision=1, activated_at=stamp, activation_kind="stock_start")
        db.add(state)
    else:
        state.revision += 1
        state.activated_at, state.activation_kind, state.cutover_id = stamp, "stock_start", None
    await db.flush()
    return tracking
