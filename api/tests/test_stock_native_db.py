"""Native evidence, replay and physical separation in isolated PostgreSQL."""
import copy
from decimal import Decimal
import unittest
from uuid import uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

import test_order_processing_db as fixture
from test_order_stock import NOW, raw_line
from inventory_hub.db_models import Product
from inventory_hub.db_models_ext import ShopOrder, ShopProduct, StockBalance
from inventory_hub.order_collection_models import OrderInboxEntry
from inventory_hub.order_exception_types import ResolutionConfirmRequest, ResolutionPreviewRequest
from inventory_hub.order_processing_models import OrderProcessingJob
from inventory_hub.services import stock_native as service, order_processing, order_exceptions
from inventory_hub.stock_native_models import StockNativeEvent, StockNativeLine, StockNativeOrder
from inventory_hub.stock_publication_models import StockPublicationHold


@unittest.skipUnless(fixture.TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class NativeDatabaseTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixture.OrderProcessingDatabaseTests.asyncTearDown
    transaction = fixture.OrderProcessingDatabaseTests.transaction
    client = fixture.OrderProcessingDatabaseTests.client
    raw = fixture.OrderProcessingDatabaseTests.raw
    seed = fixture.OrderProcessingDatabaseTests.seed
    observe = fixture.OrderProcessingDatabaseTests.observe
    start = fixture.OrderProcessingDatabaseTests.start
    fetch = fixture.OrderProcessingDatabaseTests.fetch
    finish = fixture.OrderProcessingDatabaseTests.finish
    process = fixture.OrderProcessingDatabaseTests.process
    state = fixture.OrderProcessingDatabaseTests.state
    physical_snapshot = fixture.OrderProcessingDatabaseTests.physical_snapshot
    manual_preview = fixture.OrderProcessingDatabaseTests.manual_preview
    manual_apply = fixture.OrderProcessingDatabaseTests.manual_apply

    async def asyncSetUp(self):
        await fixture.OrderProcessingDatabaseTests.asyncSetUp(self)
        async with self.transaction() as db:
            db.add(ShopProduct(shop_id=self.shops['biketrek'], product_id=self.product_id, external_code='SKU-A', is_variant=False, external_product_id='101'))

    async def native(self):
        async with self.sessions() as db:
            total = await service.projection(db, self.shops["biketrek"], self.warehouse_id, self.product_id)
            current = await db.scalar(select(StockNativeOrder))
            count = await db.scalar(select(func.count()).select_from(StockNativeEvent))
            return total, current, count

    async def hold(self, assertions=None):
        identifier = str(uuid4())
        async with self.transaction() as db:
            db.add(StockPublicationHold(id=identifier, shop_id=self.shops["biketrek"],
                warehouse_id=self.warehouse_id, active=True, assertions=assertions if assertions is not None else
                {"external_writers_paused": True, "orders_reconciled": True, "confirmed": True}))
        return identifier

    async def plan(self, skus=None):
        async with self.sessions() as db:
            return await service.maintenance_plan(db, self.shops["biketrek"], self.warehouse_id, skus or ["SKU-A"])

    async def test_reserve_issue_and_retry_do_not_duplicate_native_debit(self):
        await self.seed("5")
        raw = self.raw(raw_line(product_id=101, quantity="2"))
        await self.process(raw)
        self.assertEqual((await self.native())[0], Decimal(-2))
        first = await self.native()
        await self.process()
        self.assertEqual((await self.native())[2], first[2])
        raw.update(status_id=8, last_update_time=NOW.isoformat())
        await self.process(raw)
        self.assertEqual((await self.native())[0], Decimal(-2))
        state = await self.state()
        self.assertEqual((state.balance.qty_on_hand, state.balance.qty_reserved), (3, 0))
        self.assertEqual(len(state.movements), 1)

    async def test_backorder_native_debt_is_full_quantity_not_covered_stock(self):
        await self.seed("2")
        await self.process(self.raw(raw_line(product_id=101, quantity="5")))
        total, _, count = await self.native()
        self.assertEqual(total, Decimal(-5))
        self.assertEqual((await self.state()).balance.qty_reserved, 2)
        await self.process()
        self.assertEqual((await self.native())[2], count)

    async def test_cancel_releases_native_debt_and_reopen_stays_fenced(self):
        await self.seed("5")
        raw = self.raw(raw_line(product_id=101, quantity="2"))
        await self.process(raw)
        raw.update(status_id=2, last_update_time=NOW.isoformat())
        await self.process(raw)
        self.assertEqual((await self.native())[0], 0)
        raw.update(status_id=1)
        await self.process(raw)
        with self.assertRaisesRegex(service.NativeError, "stock_native_reopened_unknown"):
            await self.native()
        raw.update(status_id=2)
        await self.process(raw)
        with self.assertRaisesRegex(service.NativeError, "stock_native_reopened_unknown"):
            await self.native()

    async def test_first_seen_cancellation_and_changed_canceled_quantity_stay_fenced(self):
        await self.seed("5")
        raw = self.raw(raw_line(product_id=101, quantity="2"), status_id=2)
        await self.process(raw)
        with self.assertRaisesRegex(service.NativeError, "stock_native_cancellation_unknown"):
            await self.native()
        raw.update(status_id=1, last_update_time=NOW.isoformat())
        await self.process(raw)
        with self.assertRaisesRegex(service.NativeError, "stock_native_cancellation_unknown"):
            await self.native()

    async def test_edit_of_previously_observed_canceled_lines_is_not_assumed_restored(self):
        await self.seed("5")
        raw = self.raw(raw_line(product_id=101, quantity="2"))
        await self.process(raw)
        raw.update(status_id=2, last_update_time=NOW.isoformat())
        await self.process(raw)
        self.assertEqual((await self.native())[0], 0)
        raw["products"][0]["quantity"] = "3"
        await self.process(raw)
        with self.assertRaisesRegex(service.NativeError, "stock_native_cancellation_unknown"):
            await self.native()
        raw["products"][0]["quantity"] = "2"
        await self.process(raw)
        with self.assertRaisesRegex(service.NativeError, "stock_native_cancellation_unknown"):
            await self.native()

    async def test_reviewed_cancel_after_issue_changes_evidence_without_physical_return(self):
        await self.seed("5")
        raw = self.raw(raw_line(product_id=101, quantity="2"), status_id=8)
        await self.process(raw)
        original = (await self.state()).orders[0].stock_snapshot
        before = await self.physical_snapshot()
        raw.update(status_id=2, last_update_time=NOW.isoformat())
        plan = await self.start(raw)
        with self.assertRaises(order_processing.ProcessingError) as failure:
            await self.finish(plan)
        async with self.transaction() as db:
            await order_processing.fail_job(db, plan, failure.exception.code)
        async with self.sessions() as db:
            preview = await order_exceptions.preview(db, plan["id"], ResolutionPreviewRequest(request_id=uuid4()))
        async with self.sessions() as db:
            await order_exceptions.resolve(db, preview["id"], ResolutionConfirmRequest(
                preview_hash=preview["preview_hash"], confirmed=True, stock_accounted=True,
                reason="Confirmed physical issue; no returned goods"), "test:operator")
        self.assertEqual((await self.native())[0], 0)
        self.assertEqual(await self.physical_snapshot(), before)
        self.assertEqual((await self.state()).orders[0].stock_snapshot, original)
        await self.process()
        self.assertEqual((await self.native())[0], 0)

    async def test_deleted_order_or_new_source_hash_blocks_coverage(self):
        await self.seed("5")
        await self.process(self.raw(raw_line(product_id=101)))
        async with self.transaction() as db:
            await db.execute(update(OrderInboxEntry).values(deleted=True))
        with self.assertRaisesRegex(service.NativeError, "stock_native_deleted_unknown"):
            await self.native()
        async with self.transaction() as db:
            await db.execute(update(OrderInboxEntry).values(deleted=False))
            await db.execute(update(OrderProcessingJob).values(source_hash="a" * 64))
        with self.assertRaisesRegex(service.NativeError, "stock_native_coverage_stale"):
            await self.native()

    async def test_mapping_change_and_missing_existing_evidence_block_publication(self):
        await self.seed("5")
        await self.process(self.raw(raw_line(product_id=101)))
        async with self.transaction() as db:
            await db.execute(update(ShopProduct).values(external_product_id="999"))
        with self.assertRaisesRegex(service.NativeError, "stock_native_line_identity"):
            await self.native()
        async with self.transaction() as db:
            await db.execute(update(ShopProduct).values(external_product_id="101"))
            row = await db.scalar(select(ShopOrder))
            row.stock_revision += 1
        with self.assertRaisesRegex(service.NativeError, "stock_native_coverage_stale"):
            await self.native()

    async def test_manual_apply_proves_native_facts_without_fabricating_processor_job(self):
        await self.seed("5")
        raw = self.raw(raw_line(product_id=101, quantity="2"))
        await self.observe(raw)
        preview = await self.manual_preview(raw)
        async with self.sessions() as db:
            with self.assertRaisesRegex(service.NativeError, "stock_native_coverage_missing"):
                await service.coverage(db, self.shops["biketrek"], self.warehouse_id)
        await self.manual_apply(preview)
        self.assertEqual((await self.native())[0], Decimal(-2))
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(OrderProcessingJob)), 0)
            from inventory_hub.services import stock_settings
            from inventory_hub.services.order_sync_readiness import has_unaccounted_orders
            config = await stock_settings.effective(db, self.shops["biketrek"])
            self.assertTrue(await has_unaccounted_orders(db, self.shops["biketrek"], self.warehouse_id, config),
                            "Native evidence never bypasses independent order processing gates")

    async def test_maintenance_requires_all_products_from_native_history_and_preserves_audit(self):
        await self.seed("5")
        async with self.transaction() as db:
            from stock_tracking_fixture import confirmed_inventory
            product = Product(sku="SKU-B", name="Second native product")
            db.add(product)
            await db.flush()
            second_id = product.id
            db.add(confirmed_inventory(product.id, self.warehouse_id))
            db.add(StockBalance(product_id=product.id, warehouse_id=self.warehouse_id,
                qty_on_hand=5, qty_reserved=0, avg_cost=3, total_value=15))
            db.add(ShopProduct(shop_id=self.shops['biketrek'], product_id=product.id, external_code='SKU-B', is_variant=False, external_product_id='202'))
        raw = self.raw(raw_line(product_id=101, quantity="2"))
        await self.process(raw)
        raw["products"][0].update(code="SKU-B", product_id=202)
        raw["last_update_time"] = NOW.isoformat()
        await self.process(raw)
        raw["products"][0]["quantity"] = "3"
        raw["status_id"] = 2
        await self.process(raw)
        with self.assertRaisesRegex(service.NativeError, "stock_native_reconciliation_scope"):
            await self.plan(["SKU-B"])
        plan = await self.plan(["SKU-B", "SKU-A"])
        self.assertEqual(plan["orders"][0]["product_ids"], sorted([self.product_id, second_id]))
        async with self.sessions() as db:
            await service.coverage(db, self.shops["biketrek"], self.warehouse_id, maintenance_plan=plan)
            with self.assertRaisesRegex(service.NativeError, "stock_native_cancellation_unknown"):
                await service.coverage(db, self.shops["biketrek"], self.warehouse_id)
        hold_id = await self.hold()
        async with self.transaction() as db:
            await service.apply_maintenance(db, plan, hold_id)
        self.assertEqual((await self.native())[0], 0)
        async with self.sessions() as db:
            self.assertEqual(await service.projection(db, self.shops["biketrek"], self.warehouse_id, second_id), 0)
            current = await db.scalar(select(StockNativeOrder))
            history = await db.get(StockNativeEvent, current.event_id)
            self.assertEqual(history.facts["maintenance"]["hold_id"], hold_id)
            self.assertEqual(history.facts["maintenance"]["previous_event_id"], plan["orders"][0]["event_id"])
            self.assertIsNone(current.error)
            self.assertTrue(current.canceled_seen)

    async def test_maintenance_rollback_and_stale_plan_do_not_clear_error(self):
        await self.seed("5")
        await self.process(self.raw(raw_line(product_id=101), status_id=2))
        plan, hold_id = await self.plan(), await self.hold()
        async with self.sessions() as db:
            await service.apply_maintenance(db, plan, hold_id)
            await db.rollback()
        with self.assertRaisesRegex(service.NativeError, "stock_native_cancellation_unknown"):
            await self.native()
        changed = copy.deepcopy(plan)
        changed["orders"][0]["source_hash"] = "a" * 64
        async with self.sessions() as db:
            with self.assertRaisesRegex(service.NativeError, "stock_native_reconciliation_changed"):
                await service.apply_maintenance(db, changed, hold_id)
            await db.rollback()
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockNativeEvent)), 1)

    async def test_maintenance_clears_only_past_uncertainty_and_future_cancel_edit_fences_again(self):
        await self.seed("5")
        raw = self.raw(raw_line(product_id=101), status_id=2)
        await self.process(raw)
        plan, hold_id = await self.plan(), await self.hold()
        async with self.transaction() as db:
            await service.apply_maintenance(db, plan, hold_id)
            hold = await db.get(StockPublicationHold, hold_id)
            hold.active, hold.closed_at, hold.close_result = False, NOW, {"test": True}
        before = await self.native()
        self.assertEqual(before[0], 0)
        await self.process(raw)
        self.assertEqual((await self.native())[2], before[2], "Same-source replay retains the maintenance audit")
        raw["products"][0]["quantity"] = "2"
        raw["last_update_time"] = NOW.isoformat()
        await self.process(raw)
        with self.assertRaisesRegex(service.NativeError, "stock_native_cancellation_unknown"):
            await self.native()

    async def test_maintenance_cannot_clear_unsupported_native_details_or_unasserted_hold(self):
        await self.seed("5")
        raw = self.raw(raw_line(product_id=101, type="gift"), status_id=2)
        await self.process(raw)
        with self.assertRaisesRegex(service.NativeError, "stock_native_line_unsupported"):
            await self.plan()
        # An independently valid semantic case still needs a real paused hold.
        raw["products"][0]["type"] = "product"
        raw["last_update_time"] = NOW.isoformat()
        await self.process(raw)
        with self.assertRaisesRegex(service.NativeError, "stock_native_reconciliation_unknown"):
            await self.plan()
        empty_plan = {"shop_id": self.shops["biketrek"], "warehouse_id": self.warehouse_id,
                      "skus": ["SKU-A"], "orders": []}
        hold_id = await self.hold(assertions={})
        async with self.sessions() as db:
            with self.assertRaisesRegex(service.NativeError, "stock_native_reconciliation_hold_required"):
                await service.apply_maintenance(db, empty_plan, hold_id)

    async def test_rollback_does_not_leave_native_evidence_or_quantity(self):
        await self.seed("5")
        raw = self.raw(raw_line(product_id=101))
        plan = await self.start(raw)
        fetched = await self.fetch(plan)
        async with self.sessions() as db:
            await order_processing.finish_job(db, plan, fetched)
            await db.rollback()
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockNativeEvent)), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockNativeLine)), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockNativeOrder)), 0)

    async def test_history_is_immutable_and_migration_replay_does_not_change_evidence(self):
        await self.seed("5")
        await self.process(self.raw(raw_line(product_id=101)))
        before = await self.native()
        async with self.sessions() as db:
            with self.assertRaises(DBAPIError):
                await db.execute(text("UPDATE stock_native_events SET error = 'forged'"))
            await db.rollback()
            with self.assertRaises(DBAPIError):
                await db.execute(text("DELETE FROM stock_native_events"))
            await db.rollback()
        import asyncpg
        from pathlib import Path
        connection = await asyncpg.connect(fixture.TEST_URL)
        try:
            await connection.execute(f'SET search_path TO "{self.schema}"')
            await connection.execute((Path(__file__).resolve().parents[2] / "infra/db-init/025_stock_native.sql").read_text())
        finally:
            await connection.close()
        self.assertEqual((await self.native())[0::2], before[0::2])
