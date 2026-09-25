"""Explicit confirmation distinguishes physical stock from historical imports."""
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
    """A confirmed start cannot certify an unexplained quantity after a zero count."""
    from inventory_hub.services.stock_tracking import confirmed_stock, current_movement
    movement = exists(select(StockMovement.id).where(
        StockMovement.product_id == balance.product_id, StockMovement.warehouse_id == balance.warehouse_id,
        current_movement()))
    counted_zero = and_(balance.qty_on_hand == 0, balance.qty_reserved == 0, balance.qty_quarantined == 0)
    return and_(confirmed_stock(balance.product_id, balance.warehouse_id), or_(movement, counted_zero))
