"""A zero balance is known only after an explicit physical count or a real movement."""
from sqlalchemy import and_, exists, or_, select

from inventory_hub.db_models_ext import StockBalance, StockMovement
from inventory_hub.stock_adjustment_models import StockAdjustment


def audited_zero_count(product_id, warehouse_id):
    """Committed result of the protected recount workflow, never a prepared draft."""
    return exists(select(StockAdjustment.id).where(
        StockAdjustment.product_id == product_id, StockAdjustment.warehouse_id == warehouse_id,
        StockAdjustment.status == "completed", StockAdjustment.completed_at.is_not(None),
        StockAdjustment.preview_data["initial_zero_count"].astext == "true",
        StockAdjustment.result["initial_zero_count"].astext == "true",
        StockAdjustment.result["counted_quantity"].astext == "0",
        StockAdjustment.result["delta"].astext == "0",
        StockAdjustment.result["movement_id"].astext.is_(None)))


def physical_stock_evidence(balance=StockBalance):
    """SQL expression, evaluated with quantities in the same database snapshot."""
    movement = exists(select(StockMovement.id).where(
        StockMovement.product_id == balance.product_id, StockMovement.warehouse_id == balance.warehouse_id))
    counted_zero = and_(balance.qty_on_hand == 0, balance.qty_reserved == 0, balance.qty_quarantined == 0,
        audited_zero_count(balance.product_id, balance.warehouse_id))
    return or_(movement, counted_zero)
