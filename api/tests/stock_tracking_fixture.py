"""Explicit confirmation for existing synthetic, already-counted test stocks.

Never use this fixture to validate the start workflow itself; those tests go
through confirmed receiving/count endpoints with an initially empty registry.
"""
from inventory_hub.stock_tracking_models import StockTracking


def confirmed_inventory(product_id, warehouse_id):
    return StockTracking(product_id=product_id, warehouse_id=warehouse_id,
        source_type="test_count", source_id="synthetic-count", operator_name="test",
        historical_last_movement_id=0, historical_last_layer_id=0, archived_balance={})
