"""Automatic order stock writes in a disposable localhost PostgreSQL schema."""
import copy
import os
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.db_models import Product, Shop, Warehouse
from inventory_hub.db_models_ext import Reservation, ShopOrder, StockBalance, StockMovement
from inventory_hub.order_collection_models import OrderCollectionSettings, OrderInboxEntry
from inventory_hub.order_processing_models import OrderProcessingJob
from inventory_hub.order_stock_models import OrderStockPolicy
from inventory_hub.order_stock_types import OrderStockApplyRequest, OrderStockPreviewRequest
from inventory_hub.stock_settings_models import StockShopSettings, StockWarehouseSettings
from inventory_hub.stock_publication_models import StockPublicationHold
from inventory_hub.services import order_collection as collection
from inventory_hub.services import order_processing as service
from inventory_hub.services import order_stock as manual
from inventory_hub.services import order_stock_source as source
from inventory_hub.services import stock_settings
from test_order_stock import ACTIONS, NOW, STATUSES, raw_line, raw_order, raw_page


TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")
POLICY_START = NOW - timedelta(days=30)
AUTOMATION_START = NOW - timedelta(days=2)
ISSUE_START = NOW - timedelta(days=1)
D = Decimal


@unittest.skipUnless(TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class OrderProcessingDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("Order processing tests require a dedicated localhost *_catalog_test database")
        self.schema = "order_processing_test_" + uuid4().hex
        sql_root = Path(__file__).resolve().parents[2] / "infra" / "db-init"
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            await connection.execute((sql_root / "001_schema.sql").read_text())
            await connection.execute("CREATE TYPE payment_status AS ENUM ('unpaid', 'partial', 'paid')")
            for filename in ("002_invoice_management.sql", "007_order_stock.sql", "008_order_collection.sql",
                             "009_stock_automation.sql", "010_stock_publication.sql"):
                await connection.execute((sql_root / filename).read_text())
        finally:
            await connection.close()
        self.engine = create_async_engine(TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1),
            poolclass=NullPool, connect_args={"server_settings": {"search_path": self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        self.raw_orders, self.source_reads = {}, []
        self.config = {"upgates_api_base_url": "https://processing-test.invalid/api/v2",
                       "upgates_login": "synthetic-reader", "upgates_api_key": "test-only-secret"}
        self.patchers = [patch.object(service, "now", return_value=NOW),
                         patch.object(manual, "now", return_value=NOW),
                         patch.object(collection, "now", return_value=NOW),
                         patch.object(collection, "load_shop", side_effect=lambda code: dict(self.config)),
                         patch.object(source.UpgatesClient, "from_shop", side_effect=self.client)]
        for patcher in self.patchers:
            patcher.start()
        async with self.transaction() as db:
            self.shops = {row.code: row.id for row in (await db.scalars(select(Shop))).all()}
            warehouse = Warehouse(code="processing-central", name="Processing central")
            product = Product(sku="SKU-A", name="Processing stock fixture")
            db.add_all([warehouse, product])
            await db.flush()
            self.warehouse_id, self.product_id = warehouse.id, product.id
            normalized = source._statuses(STATUSES)
            db.add(OrderStockPolicy(shop_id=self.shops["biketrek"], warehouse_id=warehouse.id,
                starts_at=POLICY_START, revision=1, status_actions=dict(ACTIONS), **normalized))
            fingerprint = collection.target_fingerprint("biketrek")
            db.add(OrderCollectionSettings(shop_id=self.shops["biketrek"], enabled=True, revision=1,
                target_fingerprint=fingerprint, cursor_at=POLICY_START, reconcile_cursor_at=POLICY_START,
                next_poll_at=NOW))
            db.add(StockWarehouseSettings(warehouse_id=warehouse.id, revision=1, values={},
                                         processing_paused=False, updated_at=NOW))
            db.add(StockShopSettings(shop_id=self.shops["biketrek"], revision=1, overrides={}, mode="fulfill",
                automation_starts_at=AUTOMATION_START, issue_starts_at=ISSUE_START,
                authorized_policy_revision=1, target_fingerprint=fingerprint, updated_at=NOW))

    async def asyncTearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        await self.engine.dispose()
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            await connection.close()

    @asynccontextmanager
    async def transaction(self):
        async with self.sessions() as db:
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    def client(self, shop_code):
        def read(params):
            self.source_reads.append((shop_code, dict(params)))
            raw = self.raw_orders.get(params["order_numbers"])
            return raw_page(copy.deepcopy(raw)) if raw else raw_page(), copy.deepcopy(STATUSES)
        return SimpleNamespace(base_url=self.config["upgates_api_base_url"],
            read_order_audit=read, read_order_statuses=lambda: copy.deepcopy(STATUSES),
            session=SimpleNamespace(auth=(self.config["upgates_login"], "test-only-secret"), close=Mock()))

    def raw(self, *lines, **changes):
        return raw_order(*lines, **{"creation_time": (ISSUE_START + timedelta(hours=1)).isoformat(),
            "last_update_time": (NOW - timedelta(minutes=1)).isoformat(), **changes})

    async def seed(self, quantity="5"):
        async with self.transaction() as db:
            db.add(StockBalance(product_id=self.product_id, warehouse_id=self.warehouse_id,
                qty_on_hand=D(quantity), qty_reserved=D("0"), avg_cost=D("3"), total_value=D(quantity) * 3))

    async def observe(self, raw):
        self.raw_orders[raw["order_number"]] = copy.deepcopy(raw)
        observed = {"uuid": raw["uuid"], "order_number": raw["order_number"],
            "created_at": raw["creation_time"], "updated_at": raw["last_update_time"],
            "status_id": raw["status_id"], "origin": raw["origin"], "deleted": False}
        async with self.transaction() as db:
            row = await db.scalar(select(OrderInboxEntry).where(OrderInboxEntry.source_uuid == raw["uuid"]))
            values = {"created_at": datetime.fromisoformat(observed["created_at"]),
                      "updated_at": datetime.fromisoformat(observed["updated_at"]), "status_id": raw["status_id"],
                      "origin": raw["origin"], "observation_hash": source._hash(observed),
                      "observed_at": NOW, "last_seen_at": NOW}
            if row is None:
                row = OrderInboxEntry(shop_id=self.shops["biketrek"], source_uuid=raw["uuid"],
                    order_number=raw["order_number"], deleted=False, **values)
                db.add(row)
            else:
                for key, value in values.items():
                    setattr(row, key, value)

    async def start(self, raw=None):
        if raw is not None:
            await self.observe(raw)
        async with self.transaction() as db:
            await service.enqueue(db, self.shops["biketrek"], force=True)
        async with self.transaction() as db:
            identifier = await db.scalar(text("SELECT id FROM order_processing_jobs ORDER BY id LIMIT 1"))
            return await service.start_job(db, identifier)

    async def fetch(self, plan):
        async with self.sessions() as db:
            shop = await db.get(Shop, plan["shop_id"])
            return await source.load_source(db, shop, plan["order_number"],
                expected_target_fingerprint=plan["target_fingerprint"])

    async def finish(self, plan, fetched=None):
        fetched = fetched or await self.fetch(plan)
        async with self.transaction() as db:
            return await service.finish_job(db, plan, fetched)

    async def process(self, raw=None):
        plan = await self.start(raw)
        self.assertIsNotNone(plan)
        return await self.finish(plan)

    async def state(self):
        async with self.sessions() as db:
            balance = await db.scalar(select(StockBalance).where(StockBalance.product_id == self.product_id))
            orders = (await db.scalars(select(ShopOrder).order_by(ShopOrder.id))).all()
            movements = (await db.scalars(select(StockMovement).order_by(StockMovement.id))).all()
            holds = (await db.scalars(select(Reservation).order_by(Reservation.id))).all()
            jobs = (await db.execute(text("SELECT * FROM order_processing_jobs ORDER BY id"))).mappings().all()
            return SimpleNamespace(balance=balance, orders=orders, movements=movements, holds=holds, jobs=jobs)

    async def physical_snapshot(self):
        async with self.sessions() as db:
            return {table: (await db.execute(text(f"SELECT * FROM {table} ORDER BY id"))).mappings().all()
                    for table in ("stock_balances", "stock_movements", "reservations", "shop_orders",
                                  "shop_order_items", "shop_sync_outbox")}

    async def manual_preview(self, raw):
        self.raw_orders[raw["order_number"]] = copy.deepcopy(raw)
        async with self.sessions() as db:
            result = await manual.preview(db, OrderStockPreviewRequest(request_id=uuid4(), shop_code="biketrek",
                                                                       order_number=raw["order_number"]))
            return result["preview"]

    async def manual_apply(self, preview):
        async with self.transaction() as db:
            return await manual.apply(db, preview["id"], OrderStockApplyRequest(
                preview_hash=preview["preview_hash"], confirmed=True, physical_confirmed=True))

    async def test_unchanged_holds_and_waiting_backorders_do_not_churn_revision(self):
        await self.seed("2")
        raw = self.raw(raw_line(quantity="3"))
        await self.process(raw)
        first = await self.state()
        self.assertEqual((first.balance.qty_on_hand, first.balance.qty_reserved), (D("2"), D("2")))
        self.assertEqual(first.holds[0].shortage_qty, D("1"))
        before = await self.physical_snapshot()
        await self.process()
        self.assertEqual(await self.physical_snapshot(), before)
        self.assertEqual((await self.state()).orders[0].stock_revision, first.orders[0].stock_revision)
        self.assertEqual(first.movements, [])

    async def test_receipt_reallocates_unchanged_backorder_without_changing_source(self):
        await self.seed("1")
        raw = self.raw(raw_line(quantity="3"))
        await self.process(raw)
        before = await self.state()
        async with self.transaction() as db:
            # Simulate a separately committed receipt; the processor must plan fresh balances.
            await db.execute(update(StockBalance).where(StockBalance.product_id == self.product_id)
                .values(qty_on_hand=D("4"), total_value=D("12")))
        await self.process()
        after = await self.state()
        self.assertEqual((after.balance.qty_on_hand, after.balance.qty_reserved), (D("4"), D("3")))
        self.assertEqual(after.holds[0].shortage_qty, D("0"))
        self.assertEqual(after.orders[0].stock_revision, before.orders[0].stock_revision + 1)
        self.assertEqual(after.orders[0].stock_source_hash, before.orders[0].stock_source_hash)
        self.assertEqual(after.movements, [])

    async def test_repeated_automatic_issue_consumes_stock_and_cost_once(self):
        await self.seed("5")
        await self.process(self.raw(raw_line(quantity="2"), status_id=8))
        issued = await self.state()
        self.assertEqual((issued.balance.qty_on_hand, issued.balance.qty_reserved, issued.balance.total_value),
                         (D("3"), D("0"), D("9")))
        self.assertEqual(len(issued.movements), 1)
        self.assertEqual(issued.movements[0].total_cost, D("6"))
        before = await self.physical_snapshot()
        await self.process()
        self.assertEqual(await self.physical_snapshot(), before)

    async def test_manual_issue_wins_while_automatic_fetch_is_in_flight(self):
        await self.seed("5")
        raw = self.raw(raw_line(quantity="2"), status_id=8)
        plan = await self.start(raw)
        fetched = await self.fetch(plan)
        preview = await self.manual_preview(raw)
        await self.manual_apply(preview)
        before = await self.physical_snapshot()
        await self.finish(plan, fetched)
        self.assertEqual(await self.physical_snapshot(), before)
        self.assertEqual(len((await self.state()).movements), 1)

    async def test_automatic_issue_invalidates_older_manual_preview_without_double_issue(self):
        await self.seed("5")
        raw = self.raw(raw_line(quantity="2"), status_id=8)
        preview = await self.manual_preview(raw)
        await self.process(raw)
        before = await self.physical_snapshot()
        with self.assertRaises(manual.OrderStockError):
            await self.manual_apply(preview)
        self.assertEqual(await self.physical_snapshot(), before)
        self.assertEqual(len((await self.state()).movements), 1)

    async def assert_interruption_preserves_stock(self, mutate, expected_code):
        await self.seed("5")
        plan = await self.start(self.raw(raw_line(quantity="2"), status_id=8))
        fetched = await self.fetch(plan)
        before = await self.physical_snapshot()
        await mutate()
        with self.assertRaises(service.ProcessingError) as raised:
            await self.finish(plan, fetched)
        self.assertEqual(raised.exception.code, expected_code)
        async with self.transaction() as db:
            await service.fail_job(db, plan, raised.exception.code)
        self.assertEqual(await self.physical_snapshot(), before)
        self.assertEqual((await self.state()).jobs[0]["error"], expected_code)

    async def test_pause_during_source_fetch_blocks_inflight_issue(self):
        async def pause():
            async with self.transaction() as db:
                await db.execute(update(StockWarehouseSettings).values(processing_paused=True, revision=2))
        await self.assert_interruption_preserves_stock(pause, "order_processing_paused")

    async def test_policy_change_during_source_fetch_requires_new_authorization(self):
        async def reconfigure():
            async with self.transaction() as db:
                await db.execute(update(OrderStockPolicy).values(revision=2))
        await self.assert_interruption_preserves_stock(reconfigure, "order_processing_policy_changed")

    async def test_target_change_during_source_fetch_blocks_inflight_issue(self):
        async def change_target():
            self.config["upgates_api_base_url"] = "https://another-processing-test.invalid/api/v2"
        await self.assert_interruption_preserves_stock(change_target, "order_processing_target_changed")

    async def test_operational_settings_change_during_fetch_invalidates_inflight_configuration(self):
        async def change_settings():
            async with self.transaction() as db:
                await db.execute(update(StockShopSettings).values(revision=2, overrides={"processing_retry_minutes": 10}))
        await self.assert_interruption_preserves_stock(change_settings, "order_processing_configuration_changed")

    async def test_newest_inbox_generation_survives_older_fetch_and_late_failure(self):
        await self.seed("5")
        raw = self.raw(raw_line(quantity="2"))
        old_plan = await self.start(raw)
        old_fetched = await self.fetch(old_plan)
        before = await self.physical_snapshot()
        raw.update(status_id=8, last_update_time=NOW.isoformat())
        await self.observe(raw)
        # The inbox can advance without a queue pass while HTTP is in flight.
        self.assertEqual(await self.finish(old_plan, old_fetched), {"status": "superseded"})
        state = await self.state()
        self.assertEqual((state.jobs[0]["generation"], state.jobs[0]["status"]), (old_plan["generation"] + 1, "pending"))
        self.assertEqual(await self.physical_snapshot(), before)
        async with self.transaction() as db:
            await service.fail_job(db, old_plan, "order_processing_source_unavailable")
        self.assertEqual((await self.state()).jobs[0]["status"], "pending")
        await self.process()
        after = await self.state()
        self.assertEqual(after.orders[0].stock_state, "issued")
        self.assertEqual(after.balance.qty_on_hand, D("3"))
        self.assertEqual(len(after.movements), 1)

    async def test_queue_replacing_running_generation_prevents_old_completion(self):
        await self.seed("5")
        raw = self.raw(raw_line(quantity="2"))
        old_plan = await self.start(raw)
        old_fetched = await self.fetch(old_plan)
        raw.update(status_id=2, products=[], last_update_time=NOW.isoformat())
        await self.observe(raw)
        async with self.transaction() as db:
            await service.enqueue(db, self.shops["biketrek"], force=True)
        before = await self.physical_snapshot()
        self.assertEqual(await self.finish(old_plan, old_fetched), {"status": "superseded"})
        self.assertEqual(await self.physical_snapshot(), before)
        await self.process()
        state = await self.state()
        self.assertEqual(state.orders[0].stock_state, "cancelled")
        self.assertEqual((state.balance.qty_on_hand, state.balance.qty_reserved), (D("5"), D("0")))
        self.assertEqual(state.movements, [])

    async def test_issued_content_change_requires_review_and_never_restores_or_reissues_stock(self):
        await self.seed("5")
        raw = self.raw(raw_line(quantity="2"), status_id=8)
        await self.process(raw)
        before = await self.physical_snapshot()
        raw["products"][0]["quantity"] = "3"
        raw["last_update_time"] = NOW.isoformat()
        plan = await self.start(raw)
        with self.assertRaises(service.ProcessingError) as raised:
            await self.finish(plan)
        self.assertEqual(raised.exception.code, "order_stock_issued_locked")
        async with self.transaction() as db:
            await service.fail_job(db, plan, raised.exception.code)
        self.assertEqual(await self.physical_snapshot(), before)
        self.assertEqual((await self.state()).jobs[0]["status"], "review")

    async def test_restart_reclaims_abandoned_attempt_and_rejects_its_late_response(self):
        await self.seed("5")
        raw = self.raw(raw_line(quantity="2"), status_id=8)
        old_plan = await self.start(raw)
        fetched = await self.fetch(old_plan)
        async with self.transaction() as db:
            await service.recover(db)
        new_plan = await self.start()
        self.assertEqual(new_plan["attempt"], old_plan["attempt"] + 1)
        before = await self.physical_snapshot()
        self.assertEqual(await self.finish(old_plan, fetched), {"status": "superseded"})
        self.assertEqual(await self.physical_snapshot(), before)
        await self.finish(new_plan, fetched)
        self.assertEqual(len((await self.state()).movements), 1)

    async def test_audit_failure_rolls_back_stock_and_order_together(self):
        await self.seed("5")
        plan = await self.start(self.raw(raw_line(quantity="2"), status_id=8))
        fetched = await self.fetch(plan)
        before = await self.physical_snapshot()
        with patch.object(service, "OrderStockPreview", side_effect=RuntimeError("synthetic audit failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic audit failure"):
                await self.finish(plan, fetched)
        self.assertEqual(await self.physical_snapshot(), before)
        self.assertEqual((await self.state()).jobs[0]["status"], "running")

    async def test_rate_limit_cooldown_blocks_all_shop_wake_paths_without_changing_authority(self):
        await self.seed("5")
        plan = await self.start(self.raw(raw_line(quantity="2"), status_id=8))
        before = await self.physical_snapshot()
        async with self.sessions() as db:
            original = await stock_settings.effective(db, self.shops["biketrek"])
        async with self.transaction() as db:
            await service.fail_job(db, plan, "order_stock_rate_limited", retry_after=900)
        state = await self.state()
        self.assertEqual((state.jobs[0]["status"], state.jobs[0]["error"]), ("retry", "order_stock_rate_limited"))
        self.assertGreaterEqual(state.jobs[0]["next_attempt_at"], NOW + timedelta(seconds=900))
        async with self.sessions() as db:
            current = await stock_settings.effective(db, self.shops["biketrek"])
        self.assertEqual(current["configuration_hash"], original["configuration_hash"])
        self.assertEqual(current["retry_after_at"], NOW + timedelta(seconds=900))
        for operation in (
            lambda db: service.enqueue(db, self.shops["biketrek"], force=True),
            lambda db: service.refresh(db, "biketrek"),
            lambda db: service.start_job(db, plan["id"]),
        ):
            with self.assertRaises(service.ProcessingError) as raised:
                async with self.transaction() as db:
                    await operation(db)
            self.assertEqual((raised.exception.code, raised.exception.status), ("order_processing_rate_limited", 429))
        self.assertEqual(await self.physical_snapshot(), before)
        with patch.object(service, "now", return_value=NOW + timedelta(seconds=900)):
            await self.process()
        self.assertEqual(len((await self.state()).movements), 1)

    async def test_stale_attempt_rate_limit_preserves_new_generation_and_still_cools_down_shop(self):
        await self.seed("5")
        raw = self.raw(raw_line(quantity="2"))
        old_plan = await self.start(raw)
        raw.update(status_id=8, last_update_time=NOW.isoformat())
        await self.observe(raw)
        async with self.transaction() as db:
            await service.enqueue(db, self.shops["biketrek"], force=True)
        current = (await self.state()).jobs[0]
        async with self.transaction() as db:
            await service.fail_job(db, old_plan, "order_stock_rate_limited", retry_after=900)
        after = (await self.state()).jobs[0]
        self.assertEqual((after["generation"], after["status"], after["error"], after["result"]),
                         (current["generation"], "pending", None, current["result"]))
        self.assertEqual(after["generation"], old_plan["generation"] + 1)
        with self.assertRaises(service.ProcessingError) as raised:
            await self.start()
        self.assertEqual(raised.exception.code, "order_processing_rate_limited")
        self.assertEqual((await self.state()).movements, [])

    async def test_collector_rate_limit_blocks_processor_wake_claim_and_refresh_until_expiry(self):
        await self.seed("5")
        plan = await self.start(self.raw(raw_line(quantity="2"), status_id=8))
        async with self.transaction() as db:
            await service.fail_job(db, plan, "order_processing_source_unavailable")
            await db.execute(update(OrderCollectionSettings).where(OrderCollectionSettings.shop_id == plan["shop_id"])
                .values(last_error="order_collection_rate_limited", retry_after_at=NOW + timedelta(seconds=900)))
        before = await self.physical_snapshot()
        for operation in (
            lambda db: service.enqueue(db, plan["shop_id"], force=True),
            lambda db: service.refresh(db, "biketrek"),
            lambda db: service.start_job(db, plan["id"]),
        ):
            with self.assertRaises(service.ProcessingError) as raised:
                async with self.transaction() as db:
                    await operation(db)
            self.assertEqual((raised.exception.code, raised.exception.status), ("order_processing_rate_limited", 429))
        self.assertEqual(await self.physical_snapshot(), before)
        with patch.object(service, "now", return_value=NOW + timedelta(seconds=900)):
            await self.process()
        self.assertEqual(len((await self.state()).movements), 1)

    async def test_generic_collector_retry_does_not_block_authorized_stock_processing(self):
        await self.seed("5")
        async with self.transaction() as db:
            await db.execute(update(OrderCollectionSettings).where(OrderCollectionSettings.shop_id == self.shops["biketrek"])
                .values(last_error="order_collection_source_unavailable", retry_after_at=NOW + timedelta(hours=1)))
        await self.process(self.raw(raw_line(quantity="2"), status_id=8))
        state = await self.state()
        self.assertEqual((state.balance.qty_on_hand, state.orders[0].stock_state), (D("3"), "issued"))
        self.assertEqual(len(state.movements), 1)

    async def test_collector_cooldown_from_another_target_does_not_apply_to_authorized_target(self):
        await self.seed("5")
        async with self.transaction() as db:
            await db.execute(update(OrderCollectionSettings).where(OrderCollectionSettings.shop_id == self.shops["biketrek"])
                .values(last_error="order_collection_rate_limited", retry_after_at=NOW + timedelta(hours=1),
                        target_fingerprint="f" * 64))
        await self.process(self.raw(raw_line(quantity="2"), status_id=8))
        state = await self.state()
        self.assertEqual((state.balance.qty_on_hand, state.orders[0].stock_state), (D("3"), "issued"))
        self.assertEqual(len(state.movements), 1)

    async def test_new_header_requeues_even_when_collector_observed_before_previous_completion(self):
        await self.seed("5")
        raw = self.raw(raw_line(quantity="2"))
        await self.process(raw)
        previous = (await self.state()).jobs[0]
        self.assertEqual(previous["status"], "completed")
        before = await self.physical_snapshot()
        raw.update(status_id=8, last_update_time=NOW.isoformat())
        await self.observe(raw)
        async with self.transaction() as db:
            # A collector may observe a change, wait for the processor's inbox
            # lock, and commit after processing finishes with an earlier clock.
            await db.execute(update(OrderInboxEntry).where(OrderInboxEntry.source_uuid == raw["uuid"])
                .values(observed_at=previous["updated_at"] - timedelta(seconds=1)))
        async with self.transaction() as db:
            self.assertEqual(await service.enqueue(db, self.shops["biketrek"], force=False), 1)
        queued = (await self.state()).jobs[0]
        self.assertEqual((queued["generation"], queued["status"]), (previous["generation"] + 1, "pending"))
        self.assertNotEqual(queued["observation_hash"], previous["observation_hash"])
        self.assertEqual(await self.physical_snapshot(), before)
        async with self.transaction() as db:
            self.assertEqual(await service.enqueue(db, self.shops["biketrek"], force=False), 0)
        self.assertEqual((await self.state()).jobs[0]["generation"], queued["generation"])

    async def test_same_hash_review_rows_do_not_starve_later_new_orders_in_bounded_queue(self):
        reason = "order_collection_source_conflict"
        for index in range(5):
            await self.observe(self.raw(order_number=f"OLD-{index}"))
        async with self.transaction() as db:
            await db.execute(update(StockShopSettings).where(StockShopSettings.shop_id == self.shops["biketrek"])
                .values(overrides={"processing_batch_size": 1}))
            entries = (await db.scalars(select(OrderInboxEntry).order_by(OrderInboxEntry.id))).all()
            for entry in entries:
                entry.observed_at = NOW - timedelta(minutes=1)
                entry.review_reason = reason
                db.add(OrderProcessingJob(shop_id=entry.shop_id, inbox_id=entry.id,
                    source_uuid=entry.source_uuid, order_number=entry.order_number,
                    observation_hash=entry.observation_hash, generation=1, status="completed", attempts=1,
                    next_attempt_at=NOW + timedelta(hours=24), error=None, result={"previous": entry.order_number}))
        await self.observe(self.raw(order_number="NEW-AFTER-REVIEWS"))
        before = await self.physical_snapshot()
        async with self.transaction() as db:
            self.assertEqual(await service.enqueue(db, self.shops["biketrek"], force=False), 5)
        reviewed = (await self.state()).jobs
        self.assertEqual(len(reviewed), 5)
        self.assertTrue(all(row["status"] == "review" and row["error"] == reason for row in reviewed))
        self.assertTrue(all(row["result"] == {"previous": row["order_number"]} for row in reviewed))
        async with self.transaction() as db:
            self.assertEqual(await service.enqueue(db, self.shops["biketrek"], force=False), 1)
        jobs = (await self.state()).jobs
        self.assertEqual(len(jobs), 6)
        self.assertEqual((jobs[-1]["order_number"], jobs[-1]["status"]), ("NEW-AFTER-REVIEWS", "pending"))
        self.assertEqual(await self.physical_snapshot(), before)

    async def test_same_hash_inbox_review_invalidates_running_lease_without_stock_effect(self):
        await self.seed("5")
        plan = await self.start(self.raw(raw_line(quantity="2"), status_id=8))
        fetched = await self.fetch(plan)
        before = await self.physical_snapshot()
        async with self.transaction() as db:
            await db.execute(update(OrderInboxEntry).where(OrderInboxEntry.source_uuid == plan["source_uuid"])
                .values(review_reason="order_collection_source_conflict"))
        async with self.transaction() as db:
            self.assertEqual(await service.enqueue(db, self.shops["biketrek"], force=False), 1)
        job = (await self.state()).jobs[0]
        self.assertEqual((job["generation"], job["status"], job["error"]),
                         (plan["generation"] + 1, "review", "order_collection_source_conflict"))
        self.assertEqual(job["observation_hash"], plan["observation_hash"])
        self.assertEqual(await self.finish(plan, fetched), {"status": "superseded"})
        self.assertEqual(await self.physical_snapshot(), before)

    async def test_publication_hold_blocks_automatic_work_and_schedules_existing_attempt_for_retry(self):
        await self.seed("5")
        plan = await self.start(self.raw(raw_line(quantity="2"), status_id=8))
        before = await self.physical_snapshot()
        async with self.transaction() as db:
            db.add(StockPublicationHold(id=str(uuid4()), shop_id=self.shops["biketrek"],
                warehouse_id=self.warehouse_id, active=True,
                assertions={"external_writers_paused": True, "orders_reconciled": True}, created_at=NOW))
        for operation in (
            lambda db: service.enqueue(db, plan["shop_id"], force=True),
            lambda db: service.refresh(db, "biketrek"),
            lambda db: service.start_job(db, plan["id"]),
        ):
            with self.assertRaises(service.ProcessingError) as raised:
                async with self.transaction() as db:
                    await operation(db)
            self.assertEqual((raised.exception.code, raised.exception.status), ("stock_publication_warehouse_held", 409))
        async with self.transaction() as db:
            await service.fail_job(db, plan, "stock_publication_warehouse_held")
        state = await self.state()
        self.assertEqual((state.jobs[0]["status"], state.jobs[0]["error"]), ("retry", "stock_publication_warehouse_held"))
        self.assertGreater(state.jobs[0]["next_attempt_at"], NOW)
        self.assertEqual(self.source_reads, [])
        self.assertEqual(await self.physical_snapshot(), before)
