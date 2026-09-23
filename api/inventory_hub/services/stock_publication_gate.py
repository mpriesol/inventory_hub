"""Transaction-scoped writer admission to a persistently held warehouse."""
from sqlalchemy import select
from inventory_hub.db_models import Warehouse
from inventory_hub.stock_publication_models import StockPublicationHold


class StockPublicationHoldError(Exception):
    code = "stock_publication_warehouse_held"
    status = 409

    def __init__(self):
        super().__init__(self.code)


async def require_stock_write_allowed(db, warehouse_id: int):
    # Hold creation takes UPDATE on the same row. Keeping SHARE until commit
    # drains admitted writers before a hold can become visible. Always precede
    # balance insertion/locking; checking the hold alone would race its creation.
    await db.execute(select(Warehouse.id).where(Warehouse.id == warehouse_id).with_for_update(read=True))
    held = await db.scalar(select(StockPublicationHold.id).where(
        StockPublicationHold.warehouse_id == warehouse_id,
        StockPublicationHold.active.is_(True),
    ).limit(1))
    if held is not None:
        raise StockPublicationHoldError()
