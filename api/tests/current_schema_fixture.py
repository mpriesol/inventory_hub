"""Core schema required by the current Product and ShopProduct ORM models.

Feature tests retain their explicit feature migrations. These additive core
upgrades run before fixtures, while dedicated historical migration tests keep
constructing their original schemas explicitly.
"""
from pathlib import Path


CORE_MIGRATIONS = (
    "004_shop_product_content.sql",
    "011_fifo.sql",
    "019_stock_tracking.sql",
    "030_shop_identity.sql",
    "031_item_units.sql",
    "032_supplier_authority.sql",
    "035_order_webhooks.sql",
)


async def apply_current_core_schema(connection):
    root = Path(__file__).resolve().parents[2] / "infra" / "db-init"
    for filename in CORE_MIGRATIONS:
        await connection.execute((root / filename).read_text(encoding="utf-8"))
