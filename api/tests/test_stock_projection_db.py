"""Stock drafts in a disposable localhost PostgreSQL schema, never production."""
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import unittest
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.db_models import MovementType, Product, Shop, Warehouse
from inventory_hub.db_models_ext import ShopProduct, StockBalance, StockMovement
from inventory_hub.order_stock_models import OrderStockPolicy
from inventory_hub.services import stock_projection as service
from stock_tracking_fixture import confirmed_inventory


TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")
D = Decimal


@unittest.skipUnless(TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class StockProjectionDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("Projection tests require a dedicated localhost *_catalog_test database")
        self.schema = "stock_projection_test_" + uuid4().hex
        sql_root = Path(__file__).resolve().parents[2] / "infra" / "db-init"
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            await connection.execute((sql_root / "001_schema.sql").read_text())
            await connection.execute((sql_root / "019_stock_tracking.sql").read_text())
            await connection.execute("CREATE TYPE payment_status AS ENUM ('unpaid', 'partial', 'paid')")
            await connection.execute((sql_root / "002_invoice_management.sql").read_text())
            await connection.execute((sql_root / "007_order_stock.sql").read_text())
            await connection.execute((sql_root / "011_fifo.sql").read_text())
            await connection.execute((sql_root / "017_stock_adjustments.sql").read_text())
        finally:
            await connection.close()
        self.engine = create_async_engine(TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1),
            poolclass=NullPool, connect_args={"server_settings": {"search_path": self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=True)
        async with self.sessions() as db:
            warehouse = Warehouse(code="projection-central", name="Projection central")
            other = Warehouse(code="projection-other", name="Unselected warehouse")
            products = [Product(sku=sku, name=sku) for sku in ("SKU-A", "SKU-B", "EMPTY")]
            db.add_all([warehouse, other, *products])
            await db.flush()
            self.warehouse_id, self.other_id = warehouse.id, other.id
            self.products = {product.sku: product.id for product in products}
            self.shops = {shop.code: shop.id for shop in (await db.scalars(select(Shop))).all()}
            for shop_code in ("biketrek", "xtrek"):
                db.add(OrderStockPolicy(shop_id=self.shops[shop_code], warehouse_id=warehouse.id,
                    starts_at=datetime.now(timezone.utc), revision=1, status_actions={}, status_hash="a" * 64, statuses=[]))
                for product in products:
                    parent = "xTrek" if shop_code == "biketrek" else "NORMAL-" + product.sku
                    db.add(ShopProduct(shop_id=self.shops[shop_code], product_id=product.id,
                        external_code=parent, variant_code=product.sku, parent_code=parent, is_variant=True))
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            await connection.close()

    async def seed(self, sku, on_hand="7", reserved="3", *, evidence=True, warehouse_id=None):
        warehouse_id = warehouse_id or self.warehouse_id
        async with self.sessions() as db:
            db.add(StockBalance(product_id=self.products[sku], warehouse_id=warehouse_id,
                qty_on_hand=D(on_hand), qty_reserved=D(reserved), avg_cost=D("1"), total_value=D(on_hand)))
            if evidence:
                db.add(confirmed_inventory(self.products[sku], warehouse_id))
                quantity = D(on_hand) if D(on_hand) > 0 else D("1")
                db.add(StockMovement(idempotency_key=uuid4().hex, product_id=self.products[sku], warehouse_id=warehouse_id,
                    movement_type=MovementType.INITIAL, quantity=quantity, unit_cost=D("1"),
                    balance_after=quantity, avg_cost_after=D("1")))
                if D(on_hand) == 0:
                    db.add(StockMovement(idempotency_key=uuid4().hex, product_id=self.products[sku], warehouse_id=warehouse_id,
                        movement_type=MovementType.SALE_OUT, quantity=-quantity, unit_cost=D("1"),
                        balance_after=D("0"), avg_cost_after=D("1")))
            await db.commit()

    async def preview(self, shop="biketrek", skus=None):
        async with self.sessions() as db:
            return await service.preview(db, shop, skus or ["SKU-A"])

    async def snapshot(self):
        async with self.sessions() as db:
            return {table: (await db.execute(text(f"SELECT * FROM {table} ORDER BY id"))).mappings().all()
                    for table in ("products", "shop_products", "stock_balances", "stock_movements", "shop_sync_outbox")}

    async def test_shared_sku_has_same_quantity_under_different_shop_parents_without_mutation(self):
        await self.seed("SKU-A")
        await self.seed("SKU-A", "100", "0", warehouse_id=self.other_id)
        before = await self.snapshot()
        biketrek, xtrek = await self.preview(), await self.preview("xtrek")
        self.assertEqual(biketrek["rows"][0]["target"]["parent_code"], "xTrek")
        self.assertEqual(xtrek["rows"][0]["target"]["parent_code"], "NORMAL-SKU-A")
        self.assertEqual(biketrek["rows"][0]["qty_available"], "4")
        self.assertEqual(xtrek["rows"][0]["qty_available"], "4")
        self.assertEqual(await self.snapshot(), before)

    async def test_unopened_unverified_and_exhausted_stock_remain_distinct(self):
        await self.seed("SKU-A", "0", "0", evidence=False)
        await self.seed("SKU-B", "0", "0")
        rows = (await self.preview(skus=["EMPTY", "SKU-A", "SKU-B"]))["rows"]
        self.assertEqual(rows[0]["errors"], ["stock_projection_balance_missing"])
        self.assertEqual(rows[1]["errors"], ["stock_projection_balance_unverified"])
        self.assertTrue(all(row["qty_available"] is None for row in rows[:2]))
        self.assertTrue(rows[2]["quantity_known"])
        self.assertEqual(rows[2]["qty_available"], "0")

    async def test_other_warehouse_history_cannot_validate_selected_warehouse_balance(self):
        await self.seed("SKU-A", "0", "0", evidence=False)
        await self.seed("SKU-A", "100", "0", warehouse_id=self.other_id)
        row = (await self.preview())["rows"][0]
        self.assertEqual(row["errors"], ["stock_projection_balance_unverified"])
        self.assertIsNone(row["qty_available"])

    async def test_alias_and_duplicate_leaf_in_same_shop_are_blocked(self):
        await self.seed("SKU-A")
        async with self.sessions() as db:
            other_mapping = await db.scalar(select(ShopProduct).where(ShopProduct.shop_id == self.shops["biketrek"],
                                                                      ShopProduct.product_id == self.products["SKU-B"]))
            other_mapping.variant_code = "sku-a"
            await db.commit()
        self.assertEqual((await self.preview())["rows"][0]["errors"], ["stock_projection_mapping_ambiguous"])
        self.assertTrue((await self.preview("xtrek"))["rows"][0]["quantity_known"])
        async with self.sessions() as db:
            mapping = await db.scalar(select(ShopProduct).where(ShopProduct.shop_id == self.shops["xtrek"],
                                                                ShopProduct.product_id == self.products["SKU-A"]))
            mapping.variant_code = "OTHER-CODE"
            await db.commit()
        self.assertEqual((await self.preview("xtrek"))["rows"][0]["errors"], ["stock_projection_mapping_alias"])

    async def test_unmapped_canonical_case_duplicate_blocks_exact_mapped_sku(self):
        await self.seed("SKU-A")
        self.assertTrue((await self.preview())["rows"][0]["quantity_known"])
        async with self.sessions() as db:
            db.add(Product(sku="sku-a", name="Unmapped conflicting canonical code"))
            await db.commit()
        for shop in ("biketrek", "xtrek"):
            row = (await self.preview(shop))["rows"][0]
            self.assertEqual(row["errors"], ["stock_projection_product_ambiguous"])
            self.assertFalse(row["quantity_known"])
            self.assertIsNone(row["qty_available"])

    async def test_read_does_not_autoflush_or_overwrite_staged_orm_changes(self):
        await self.seed("SKU-A")
        async with self.sessions() as db:
            product = await db.get(Product, self.products["SKU-A"])
            product.name = "Pending caller edit"
            staged = Product(sku="SHOULD-NOT-EXIST", name="Pending insert")
            db.add(staged)
            result = await service.preview(db, "biketrek", ["SKU-A", "SHOULD-NOT-EXIST"])
            self.assertEqual(result["rows"][1]["errors"], ["stock_projection_product_missing"])
            self.assertIsNone(staged.id)
            self.assertEqual(product.name, "Pending caller edit")
            self.assertIn(staged, db.new)
            self.assertIn(product, db.dirty)
            await db.rollback()

    async def test_fractional_balances_and_inactive_warehouse_do_not_publish(self):
        await self.seed("SKU-A", "2.5", "0")
        row = (await self.preview())["rows"][0]
        self.assertEqual(row["errors"], ["stock_projection_unit_unsupported"])
        async with self.sessions() as db:
            warehouse = await db.get(Warehouse, self.warehouse_id)
            warehouse.is_active = False
            await db.commit()
        with self.assertRaises(service.StockProjectionError) as raised:
            await self.preview()
        self.assertEqual(raised.exception.code, "stock_projection_warehouse_unavailable")


    async def test_unicode_casefold_collisions_outside_selected_product_are_not_lost(self):
        await self.seed("SKU-A")
        async with self.sessions() as db:
            product = await db.get(Product, self.products["SKU-A"])
            product.sku = "STRASSE"
            selected = await db.scalar(select(ShopProduct).where(ShopProduct.shop_id == self.shops["biketrek"],
                                                                  ShopProduct.product_id == product.id))
            selected.variant_code = "STRASSE"
            other = await db.scalar(select(ShopProduct).where(ShopProduct.shop_id == self.shops["biketrek"],
                                                               ShopProduct.product_id == self.products["SKU-B"]))
            other.variant_code = "Straße"
            await db.commit()
        row = (await self.preview(skus=["STRASSE"]))["rows"][0]
        self.assertEqual(row["errors"], ["stock_projection_mapping_ambiguous"])
        async with self.sessions() as db:
            other = await db.scalar(select(ShopProduct).where(ShopProduct.shop_id == self.shops["biketrek"],
                                                               ShopProduct.product_id == self.products["SKU-B"]))
            other.variant_code = "SKU-B"
            db.add(Product(sku="Straße", name="Unmapped Unicode collision"))
            await db.commit()
        row = (await self.preview(skus=["STRASSE"]))["rows"][0]
        self.assertEqual(row["errors"], ["stock_projection_product_ambiguous"])
