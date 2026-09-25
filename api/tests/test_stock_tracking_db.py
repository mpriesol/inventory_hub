"""Physical starts must never inherit old imported/test quantities or costs."""
import unittest
from decimal import Decimal as D
from pathlib import Path
from uuid import uuid4

import asyncpg
from sqlalchemy import func, select, text
import test_fifo_db as fixture
from inventory_hub.db_models_ext import ShopProduct, StockBalance, StockMovement
from inventory_hub.fifo_models import FifoLayer
from inventory_hub.order_stock_models import OrderStockPolicy
from inventory_hub.stock_tracking_models import StockTracking
from inventory_hub.stock_adjustment_types import StockAdjustmentApply, StockAdjustmentPreview
from inventory_hub.routers.stock import stock_summary, product_detail
from inventory_hub.services import fifo, fifo_cost_projection, order_stock_ledger, product_editor
from inventory_hub.services import stock_adjustments, stock_history, stock_projection, stock_tracking


@unittest.skipUnless(fixture.TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class StockTrackingDatabaseTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixture.FifoDatabaseTests.asyncTearDown
    transaction = fixture.FifoDatabaseTests.transaction
    receive = fixture.FifoDatabaseTests.receive
    finalize_receipt = fixture.FifoDatabaseTests.finalize_receipt
    open_stock = fixture.FifoDatabaseTests.open_stock
    seed_legacy = fixture.FifoDatabaseTests.seed_legacy
    line = fixture.FifoDatabaseTests.line
    order = fixture.FifoDatabaseTests.order

    async def asyncSetUp(self):
        await fixture.FifoDatabaseTests.asyncSetUp(self)
        connection = await asyncpg.connect(fixture.TEST_URL)
        try:
            await connection.execute(f'SET search_path TO "{self.schema}"')
            sql_root = Path(__file__).resolve().parents[2] / "infra/db-init"
            for name in ("004_shop_product_content.sql", "012_product_editor.sql",
                         "014_supplier_availability.sql", "017_stock_adjustments.sql"):
                await connection.execute((sql_root / name).read_text())
        finally:
            await connection.close()
        async with self.transaction() as db:
            db.add(OrderStockPolicy(shop_id=self.shop_id, warehouse_id=self.warehouse_id,
                starts_at=fixture.NOW, revision=1, status_actions={}, status_hash="a" * 64, statuses=[]))
            for sku, product_id in self.products.items():
                db.add(ShopProduct(shop_id=self.shop_id, product_id=product_id, external_code=sku,
                    is_variant=False))

    async def old_receipt(self):
        """Represent a pre-019 receipt, including its original FIFO layers."""
        identifier = await self.receive(quantity="100", cost="99")
        async with self.transaction() as db:
            tracking = await db.get(StockTracking, (self.products["SKU-A"], self.warehouse_id))
            await db.delete(tracking)  # Synthetic pre-migration fixture only.
            layer_id = await db.scalar(select(FifoLayer.id))
        return identifier, layer_id

    async def count(self, quantity, *, apply=True):
        payload = StockAdjustmentPreview(request_id=uuid4(), sku="SKU-A", warehouse_code="fifo-central",
            counted_quantity=quantity, counted_at=fixture.NOW, source_reference="Physical count",
            operator_name="Test operator", reason="Start confirmed inventory")
        async with self.transaction() as db:
            prepared = await stock_adjustments.preview(db, payload)
        if not apply:
            return prepared
        async with self.transaction() as db:
            return await stock_adjustments.apply(db, prepared["id"], StockAdjustmentApply(
                preview_hash=prepared["preview_hash"], confirmed=True, quantities_verified=True, costs_documented=True))

    async def test_deployment_and_rerun_leave_old_stocks_unconfirmed_and_unsendable(self):
        await self.old_receipt()
        async with self.transaction() as db:
            before = (await db.execute(text("SELECT * FROM stock_movements ORDER BY id"))).mappings().all()
            raw = await (await db.connection()).get_raw_connection()
            await raw.driver_connection.execute((Path(__file__).resolve().parents[2] / "infra/db-init/019_stock_tracking.sql").read_text())
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockTracking)), 0)
            self.assertEqual((await stock_summary(db))["on_hand_total"], 0)
            self.assertEqual((await stock_summary(db))["confirmed_products"], 0)
            active = await product_editor.list_products(db, warehouse_code="fifo-central", stock_scope="confirmed")
            self.assertEqual(active["total"], 0)
            row = await product_editor.detail(db, self.products["SKU-A"], "fifo-central")
            self.assertEqual(row["stock"]["tracking_status"], "unconfirmed")
            self.assertIsNone(row["stock"]["qty_on_hand"])
            legacy_detail = await product_detail("SKU-A", db)
            self.assertFalse(legacy_detail["stock"]["known"])
            self.assertIsNone(legacy_detail["stock"]["on_hand"])
            projected = (await stock_projection.preview(db, "biketrek", ["SKU-A"]))["rows"][0]
            self.assertFalse(projected["quantity_known"])
            self.assertIsNone(projected["qty_available"])
            with self.assertRaisesRegex(fifo_cost_projection.FifoCostProjectionError, "stock_unconfirmed"):
                await fifo_cost_projection.product_cost(db, self.products["SKU-A"], self.warehouse_id)
            from inventory_hub.db_models import Shop
            shop = await db.get(Shop, self.shop_id)
            source = self.order()
            order = await order_stock_ledger.get_or_create_order(db, shop, source, self.warehouse_id)
            plan = await order_stock_ledger.plan_order(db, order, source, "reserve", self.warehouse_id, lock=True)
            self.assertFalse(plan["ready"])
            self.assertIn("stock_unconfirmed", str(plan["errors"]))
            self.assertEqual((await db.execute(text("SELECT * FROM stock_movements ORDER BY id"))).mappings().all(), before)

    async def test_old_100_plus_new_3_is_3_and_replays_cannot_restore_old_stock(self):
        old_receipt, old_layer_id = await self.old_receipt()
        new_receipt = await self.receive(quantity="3", cost="7")
        await self.finalize_receipt(new_receipt)
        await self.finalize_receipt(old_receipt)
        async with self.transaction() as db:
            view = await fifo.stock(db, self.products["SKU-A"], "fifo-central")
            self.assertEqual(D(view["balance"]["qty_on_hand"]), D("3"))
            self.assertEqual(D(view["valuation"]["total_value"]), D("21"))
            self.assertEqual(len(view["layers"]), 1)
            self.assertNotEqual(view["layers"][0]["id"], old_layer_id)
            self.assertEqual((await db.get(FifoLayer, old_layer_id)).quantity_remaining, D("100"))
            current = await stock_history.list_movements(db, tracking_scope="current")
            historical = await stock_history.list_movements(db, tracking_scope="historical")
            self.assertEqual([D(row["quantity"]) for row in current["items"]], [D("3")])
            self.assertEqual(sorted(D(row["quantity"]) for row in historical["items"]), [D("-100"), D("100")])
            self.assertEqual((await stock_summary(db))["on_hand_total"], 3)
            projected = (await stock_projection.preview(db, "biketrek", ["SKU-A"]))["rows"][0]
            self.assertEqual(D(projected["qty_available"]), D("3"))
            cost = await fifo_cost_projection.product_cost(db, self.products["SKU-A"], self.warehouse_id)
            self.assertNotEqual(cost["layer_id"], old_layer_id)
            self.assertEqual(D(cost["unit_cost"]), D("7"))
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockTracking)), 1)
            from inventory_hub.services import fifo_returns
            with self.assertRaisesRegex(fifo_returns.FifoReturnError, "stock_tracking_historical_layer"):
                await fifo_returns._root(db, old_layer_id)

    async def test_positive_recount_starts_at_counted_quantity_and_does_not_confirm_other_warehouse(self):
        await self.seed_legacy(sku="SKU-A", quantity="100", confirmed=False)
        from inventory_hub.db_models import Warehouse
        async with self.transaction() as db:
            other = Warehouse(code="uncounted-other", name="Uncounted other")
            db.add(other)
            await db.flush()
            db.add(StockBalance(product_id=self.products["SKU-A"], warehouse_id=other.id,
                qty_on_hand=D("200"), avg_cost=D("2"), total_value=D("400")))
        result = await self.count("5")
        self.assertEqual(D(result["delta"]), D("5"))
        async with self.transaction() as db:
            self.assertEqual((await stock_summary(db))["on_hand_total"], 5)
            other_view = await product_editor.detail(db, self.products["SKU-A"], "uncounted-other")
            self.assertFalse(other_view["stock"]["known"])

    async def test_confirmed_zero_replaces_old_100_and_remains_distinct_from_uncounted(self):
        await self.old_receipt()
        prepared = await self.count("0", apply=False)
        async with self.transaction() as db:
            self.assertFalse(await stock_tracking.is_confirmed(db, self.products["SKU-A"], self.warehouse_id))
            result = await stock_adjustments.apply(db, prepared["id"], StockAdjustmentApply(
                preview_hash=prepared["preview_hash"], confirmed=True, quantities_verified=True, costs_documented=True))
            replay = await stock_adjustments.apply(db, prepared["id"], StockAdjustmentApply(
                preview_hash=prepared["preview_hash"], confirmed=True, quantities_verified=True, costs_documented=True))
            self.assertEqual(result, replay)
            self.assertTrue(result["initial_zero_count"])
            rows = (await stock_projection.preview(db, "biketrek", ["SKU-A", "UNOPENED"]))["rows"]
            self.assertTrue(rows[0]["quantity_known"])
            self.assertEqual(D(rows[0]["qty_available"]), D("0"))
            self.assertFalse(rows[1]["quantity_known"])
            active = await product_editor.list_products(db, warehouse_code="fifo-central", stock_scope="confirmed")
            positive = await product_editor.list_products(db, warehouse_code="fifo-central", stock_scope="in_stock")
            self.assertEqual((active["total"], positive["total"]), (1, 0))
            self.assertEqual((await stock_summary(db))["confirmed_products"], 1)
            self.assertEqual((await fifo.stock(db, self.products["SKU-A"], "fifo-central"))["layers"], [])

    async def test_opening_and_recount_replace_unconfirmed_quantity_then_adjust_confirmed_quantity(self):
        await self.seed_legacy(sku="SKU-A", quantity="100", confirmed=False)
        await self.open_stock(quantity="5", cost="7")
        result = await self.count("3")
        self.assertEqual(D(result["delta"]), D("-2"))
        self.assertEqual(D(result["valuation"]["total_value"]), D("21"))

    async def test_failed_activation_rolls_back_and_old_commitments_block_start(self):
        await self.seed_legacy(sku="SKU-A", quantity="100", confirmed=False)
        with self.assertRaisesRegex(RuntimeError, "synthetic rollback"):
            async with self.transaction() as db:
                _, _, balance = await fifo._product_scope(db, sku="SKU-A", warehouse_code="fifo-central", lock=True)
                await stock_tracking.activate(db, balance, source_type="test", source_id="rollback", operator_name="test")
                raise RuntimeError("synthetic rollback")
        async with self.transaction() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockTracking)), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)), 1)
            balance = await db.scalar(select(StockBalance).where(StockBalance.product_id == self.products["SKU-A"]))
            self.assertEqual(balance.qty_on_hand, D("100"))
            balance.qty_reserved = D("1")
        with self.assertRaisesRegex(fifo.FifoError, "stock_tracking_committed_stock"):
            await self.count("0")
