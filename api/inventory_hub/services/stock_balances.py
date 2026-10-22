"""Shared transaction-scoped balance locking for physical stock writers."""
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.db_models_ext import StockBalance
from inventory_hub.db_models import Product
from inventory_hub.services.stock_publication_gate import require_stock_write_allowed, StockPublicationHoldError


class StockItemRequiredError(StockPublicationHoldError):
    code = "stock_item_required"


async def lock_stock_balances(
    db: AsyncSession, product_ids: set[int], warehouse_id: int, *, create_missing: bool = True,
) -> tuple[dict[int, StockBalance], set[int]]:
    """Lock balances in product order and report rows created by this call.

    The unique-key insert serializes writers even when no balance exists yet.
    RETURNING distinguishes a new row from any existing balance, including a
    zero balance. Callers own commit/rollback; inserted rows and locks belong
    to that same transaction. ``create_missing=False`` locks only existing
    rows: reservation-only backorders must not claim a physical balance.
    """
    await require_stock_write_allowed(db, warehouse_id)
    products = (await db.scalars(select(Product).where(Product.id.in_(sorted(product_ids)))
        .order_by(Product.id).with_for_update(read=True).execution_options(populate_existing=True))).all()
    if any(product.item_type != "stock" for product in products):
        raise StockItemRequiredError()
    balances = {}
    created_product_ids = set()
    for product_id in sorted(product_ids):
        if create_missing:
            created_id = (await db.execute(insert(StockBalance).values(
                product_id=product_id, warehouse_id=warehouse_id,
            ).on_conflict_do_nothing(
                index_elements=["product_id", "warehouse_id"],
            ).returning(StockBalance.product_id))).scalar_one_or_none()
            if created_id is not None:
                created_product_ids.add(created_id)
        balance = (await db.execute(select(StockBalance).where(
            StockBalance.product_id == product_id,
            StockBalance.warehouse_id == warehouse_id,
        ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
        if balance is not None:
            balances[product_id] = balance
    return balances, created_product_ids
