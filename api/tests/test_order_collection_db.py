"""Collector durability and worker isolation in disposable localhost PostgreSQL."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.db_models import MovementType, Product, Shop, Warehouse
from inventory_hub.db_models_ext import Reservation, ShopOrder, ShopOrderItem, ShopSyncOutbox, StockBalance, StockMovement
from inventory_hub.order_collection_models import OrderCollectionSettings, OrderCollectionRun, OrderInboxEntry
from inventory_hub.order_collection_types import CollectionConfigure, CollectionConfirmation
from inventory_hub.order_stock_models import OrderStockPolicy
from inventory_hub.services import order_collection as service, order_collection_worker as worker
from inventory_hub.services.order_collection_source import CollectionSourceError
from inventory_hub.services import stock_settings
from inventory_hub.stock_settings_types import OperationalValues, WarehouseSettingsInput, ShopSettingsInput
from inventory_hub.stock_settings_models import StockWarehouseSettings, StockShopSettings


TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")
NOW = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
CUTOVER = NOW - timedelta(days=20)


def entry(number="ORDER-01", **changes):
    return {"uuid": str(uuid4()), "order_number": number, "created_at": (CUTOVER + timedelta(days=1)).isoformat(),
            "updated_at": (NOW - timedelta(minutes=1)).isoformat(), "deleted": False,
            "origin": "frontend", "status_id": 1, **changes}


def page(*entries, number=1, pages=1, total=None):
    return {"entries": list(entries), "page": number, "number_of_pages": pages,
            "number_of_items": len(entries) if total is None else total, "has_more": number < pages}


@unittest.skipUnless(TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class OrderCollectionDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("Collector tests require a dedicated localhost *_catalog_test database")
        self.schema = "order_collection_test_" + uuid4().hex
        self.sql_root = Path(__file__).resolve().parents[2] / "infra" / "db-init"
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            await connection.execute((self.sql_root / "001_schema.sql").read_text())
            # 002 historically checks enum names outside the active schema.
            await connection.execute("CREATE TYPE payment_status AS ENUM ('unpaid', 'partial', 'paid')")
            for filename in ("002_invoice_management.sql", "007_order_stock.sql", "008_order_collection.sql",
                             "009_stock_automation.sql"):
                await connection.execute((self.sql_root / filename).read_text())
        finally:
            await connection.close()
        self.engine = create_async_engine(TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1),
            poolclass=NullPool, connect_args={"server_settings": {"search_path": self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        self.clock_patch = patch.object(service, "now", return_value=NOW)
        self.clock = self.clock_patch.start()
        self.config = {"upgates_api_base_url": "https://collector-test.invalid/api/v2",
                       "upgates_login": "synthetic-reader", "upgates_api_key": "test-only-secret"}
        self.config_patch = patch.object(service, "load_shop", side_effect=lambda code: dict(self.config))
        self.config_patch.start()
        self.engine_patch = patch.object(worker.database, "_engine", self.engine)
        self.engine_patch.start()
        self.context_patch = patch.object(worker, "get_session_context", self.transaction)
        self.context_patch.start()
        self.source_patch = patch.object(worker, "load_changed_page", AsyncMock(return_value=page()))
        self.source = self.source_patch.start()
        self.existing_uuid = str(uuid4())
        async with self.transaction() as db:
            self.shops = {shop.code: shop.id for shop in (await db.scalars(select(Shop))).all()}
            warehouse = Warehouse(code="collection-central", name="Collection central")
            product = Product(sku="COLLECTOR-SKU", name="Immutable collector stock fixture")
            db.add_all([warehouse, product])
            await db.flush()
            self.warehouse_id = warehouse.id
            db.add(OrderStockPolicy(shop_id=self.shops["biketrek"], warehouse_id=warehouse.id,
                starts_at=CUTOVER, revision=1, status_actions={"1": "reserve", "8": "issue", "2": "cancel"},
                status_hash="a" * 64, statuses=[]))
            order = ShopOrder(shop_id=self.shops["biketrek"], external_id="LEDGER-01", order_date=CUTOVER,
                stock_state="reserved", stock_revision=1, stock_warehouse_id=warehouse.id,
                stock_source_uuid=self.existing_uuid, stock_snapshot={"updated_at": CUTOVER.isoformat()})
            db.add(order)
            await db.flush()
            item = ShopOrderItem(order_id=order.id, product_id=product.id, external_item_id=str(uuid4()),
                                 quantity=Decimal("2"), stock_managed=True)
            db.add(item)
            await db.flush()
            db.add_all([
                Reservation(shop_order_item_id=item.id, product_id=product.id, warehouse_id=warehouse.id,
                            quantity=Decimal("2"), shortage_qty=Decimal("0")),
                StockBalance(product_id=product.id, warehouse_id=warehouse.id, qty_on_hand=Decimal("5"),
                             qty_reserved=Decimal("2"), avg_cost=Decimal("3"), total_value=Decimal("15")),
                StockMovement(idempotency_key="collector-initial", product_id=product.id, warehouse_id=warehouse.id,
                    movement_type=MovementType.INITIAL, quantity=Decimal("5"), unit_cost=Decimal("3"),
                    total_cost=Decimal("15"), balance_after=Decimal("5"), avg_cost_after=Decimal("3")),
                ShopSyncOutbox(shop_id=self.shops["biketrek"], product_id=product.id,
                              idempotency_key="collector-existing-outbox", sync_type="stock", payload={"stock": 3}),
            ])
        self.stock_before = await self.stock_snapshot()

    async def asyncTearDown(self):
        # Every scenario, including failure/recovery, proves collector isolation.
        try:
            self.assertEqual(await self.stock_snapshot(), self.stock_before)
        finally:
            for patcher in (self.source_patch, self.context_patch, self.engine_patch, self.config_patch, self.clock_patch):
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

    async def stock_snapshot(self):
        async with self.sessions() as db:
            return {table: (await db.execute(text(f"SELECT * FROM {table} ORDER BY id"))).mappings().all()
                    for table in ("products", "stock_balances", "stock_movements", "reservations", "shop_orders",
                                  "shop_order_items", "shop_sync_outbox")}

    async def settings(self):
        async with self.sessions() as db:
            return await db.get(OrderCollectionSettings, self.shops["biketrek"])

    async def inbox_rows(self):
        async with self.sessions() as db:
            return (await db.scalars(select(OrderInboxEntry).order_by(OrderInboxEntry.order_number))).all()

    async def runs(self):
        async with self.sessions() as db:
            return (await db.scalars(select(OrderCollectionRun).order_by(OrderCollectionRun.started_at,
                                                                         OrderCollectionRun.id))).all()

    async def configure(self, enabled=True, revision=None, shop="biketrek"):
        async with self.transaction() as db:
            return await service.configure(db, CollectionConfigure(shop_code=shop, enabled=enabled,
                expected_revision=revision, confirmed=True))

    async def refresh(self, revision):
        async with self.transaction() as db:
            return await service.refresh(db, CollectionConfirmation(shop_code="biketrek",
                expected_revision=revision, confirmed=True))

    async def operational(self, *, shop_overrides=None, **changes):
        async with self.transaction() as db:
            existing = await db.get(StockWarehouseSettings, self.warehouse_id)
            result = await stock_settings.configure_warehouse(db, WarehouseSettingsInput(
                warehouse_code="collection-central", expected_revision=existing.revision if existing else 0,
                confirmed=True, processing_paused=False, values=OperationalValues(**changes)))
        if shop_overrides is not None:
            async with self.transaction() as db:
                await stock_settings.configure_shop(db, ShopSettingsInput(shop_code="biketrek",
                    expected_revision=0, expected_warehouse_revision=result["revision"], confirmed=True,
                    overrides=shop_overrides, mode="manual"))

    async def enable(self, *, delta=False):
        await self.configure()
        if delta:
            async with self.transaction() as db:
                await db.execute(update(OrderCollectionSettings).values(last_reconciled_at=NOW))

    async def start(self):
        async with self.transaction() as db:
            return await service.start_run(db, self.shops["biketrek"])

    async def save(self, plan, *entries):
        async with self.transaction() as db:
            await service.save_page(db, plan["id"], list(entries))

    async def complete(self, plan):
        async with self.transaction() as db:
            await service.complete_run(db, plan["id"])

    async def test_collection_defaults_off_and_requires_existing_stock_policy_and_connection(self):
        async with self.transaction() as db:
            state = await service.status(db, "biketrek")
        self.assertIsNone(state["collector"])
        self.assertFalse(state["external_write_enabled"])
        self.assertFalse(await worker.cycle())
        self.source.assert_not_awaited()
        await self.configure(enabled=False)
        self.assertIsNone(await self.settings())
        with self.assertRaises(service.CollectionError) as raised:
            await self.configure(shop="xtrek")
        self.assertEqual(raised.exception.code, "order_collection_not_configured")
        self.config.pop("upgates_api_key")
        with self.assertRaises(service.CollectionError) as raised:
            await self.configure()
        self.assertEqual(raised.exception.code, "order_collection_connection_missing")
        self.assertIsNone(await self.settings())

    async def test_first_manual_refresh_is_coalesced_and_runs_once_without_enabling(self):
        response = await self.refresh(None)
        self.assertFalse(response["collector"]["enabled"])
        self.assertTrue(response["collector"]["manual_pending"])
        self.assertEqual(response["collector"]["revision"], 1)
        self.clock.return_value += timedelta(seconds=10)
        repeated = await self.refresh(1)
        self.assertEqual(repeated["collector"]["manual_requested_at"], NOW)
        with self.assertRaises(service.CollectionError):
            await self.refresh(None)
        self.assertTrue(await worker.cycle())
        row = await self.settings()
        self.assertFalse(row.enabled)
        self.assertIsNone(row.manual_requested_at)
        run = (await self.runs())[0]
        self.assertTrue(run.manual)
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.configuration_snapshot, OperationalValues().model_dump())
        self.assertEqual(len(run.configuration_hash), 64)
        async with self.sessions() as db:
            public = await service.runs(db, "biketrek")
        self.assertEqual(public["runs"][0]["trigger"], "manual")
        with self.assertRaises(service.CollectionError) as raised:
            await self.refresh(1)
        self.assertEqual(raised.exception.code, "order_collection_retry_later")
        self.clock.return_value += timedelta(days=2)
        self.source.reset_mock()
        self.assertFalse(await worker.cycle())
        self.source.assert_not_awaited()

    async def test_pending_one_off_survives_an_explicit_paused_configuration_revision(self):
        await self.refresh(None)
        await self.configure(enabled=False, revision=1)
        row = await self.settings()
        self.assertEqual(row.revision, 2)
        self.assertEqual(row.manual_requested_at, NOW)
        self.assertFalse(row.enabled)
        self.assertTrue(await worker.cycle())
        run = (await self.runs())[0]
        self.assertTrue(run.manual)
        self.assertEqual(run.settings_revision, 2)
        self.assertEqual(run.status, "completed")
        self.assertFalse((await self.settings()).enabled)

    async def test_failed_one_off_preserves_backoff_and_requires_another_explicit_request(self):
        await self.refresh(None)
        self.source.side_effect = CollectionSourceError("order_collection_rate_limited", 429, retry_after=900)
        self.assertTrue(await worker.cycle())
        state = await self.settings()
        self.assertFalse(state.enabled)
        self.assertIsNone(state.manual_requested_at)
        self.assertEqual(state.retry_after_at, NOW + timedelta(seconds=900))
        with self.assertRaises(service.CollectionError) as raised:
            await self.refresh(state.revision)
        self.assertEqual(raised.exception.code, "order_collection_retry_later")
        self.clock.return_value = state.retry_after_at
        self.source.reset_mock()
        self.assertFalse(await worker.cycle())
        self.source.assert_not_awaited()
        await self.refresh(state.revision)
        self.source.side_effect = None
        self.assertTrue(await worker.cycle())
        self.assertEqual([run.status for run in await self.runs()], ["failed", "completed"])
        self.assertFalse((await self.settings()).enabled)

    async def test_missing_policy_and_inactive_warehouse_stop_without_repeated_due_work(self):
        async with self.sessions() as db:
            state = await service.status(db, "xtrek")
        self.assertIsNone(state["configuration"])
        self.assertEqual(state["configuration_error"], "order_collection_not_configured")
        self.assertIsNone((await self.configure(shop="xtrek", enabled=False))["collector"])
        await self.refresh(None)
        async with self.transaction() as db:
            await db.execute(update(Warehouse).where(Warehouse.id == self.warehouse_id).values(is_active=False))
        self.assertFalse(await worker.cycle())
        row = await self.settings()
        self.assertFalse(row.enabled)
        self.assertEqual(row.last_error, "order_collection_not_configured")
        self.assertGreater(row.next_poll_at, NOW)
        self.assertFalse(await worker.cycle())
        self.source.assert_not_awaited()
        self.assertEqual(await self.runs(), [])
        await self.configure(enabled=False, revision=row.revision)

    async def test_shop_overrides_drive_frozen_run_limits_overlap_and_poll_delay(self):
        await self.operational(poll_interval_seconds=600, overlap_minutes=20,
            max_pages_per_pass=2, run_timeout_seconds=45,
            shop_overrides={"poll_interval_seconds": 900, "overlap_minutes": 30})
        await self.enable(delta=True)
        async with self.transaction() as db:
            await db.execute(update(OrderCollectionSettings).values(cursor_at=NOW - timedelta(hours=1)))
        plan = await self.start()
        self.assertEqual(plan["changed_from"], NOW - timedelta(minutes=90))
        self.assertEqual(plan["configuration_snapshot"]["poll_interval_seconds"], 900)
        self.assertEqual(plan["configuration_snapshot"]["max_pages_per_pass"], 2)
        self.assertEqual(plan["configuration_snapshot"]["run_timeout_seconds"], 45)
        self.assertEqual((await self.settings()).next_poll_at, NOW + timedelta(seconds=900))
        await self.complete(plan)
        self.assertEqual((await self.settings()).next_poll_at, NOW + timedelta(seconds=900))
        self.assertEqual((await self.runs())[0].configuration_snapshot, plan["configuration_snapshot"])

    async def test_configured_reconciliation_interval_and_window_control_scope(self):
        await self.operational(reconcile_interval_hours=2, reconcile_window_days=2)
        await self.enable()
        async with self.transaction() as db:
            await db.execute(update(OrderCollectionSettings).values(last_reconciled_at=NOW - timedelta(hours=3)))
        plan = await self.start()
        self.assertEqual(plan["created_from"], CUTOVER)
        self.assertEqual(plan["created_to"], CUTOVER + timedelta(days=2))
        await self.complete(plan)
        self.assertEqual((await self.settings()).reconcile_cursor_at, CUTOVER + timedelta(days=2))

    async def test_retry_uses_frozen_configured_exponential_backoff_and_maximum(self):
        await self.operational(retry_base_seconds=120, retry_max_seconds=300)
        await self.enable(delta=True)
        self.source.side_effect = CollectionSourceError("order_collection_source_unavailable")
        for delay in (120, 240, 300):
            at = self.clock.return_value
            self.assertTrue(await worker.cycle())
            row = await self.settings()
            self.assertEqual(row.retry_after_at, at + timedelta(seconds=delay))
            self.clock.return_value = row.retry_after_at

    async def test_configuration_change_during_get_discards_page_without_advancing_cursor(self):
        await self.enable(delta=True)
        async def source(shop, **params):
            await self.operational(poll_interval_seconds=600)
            return page(entry())
        self.source.side_effect = source
        self.assertTrue(await worker.cycle())
        self.source.assert_awaited_once()
        self.assertEqual(await self.inbox_rows(), [])
        self.assertEqual((await self.settings()).cursor_at, CUTOVER)
        self.assertEqual((await self.settings()).last_error, "order_collection_settings_changed")
        self.assertEqual((await self.runs())[0].configuration_snapshot["poll_interval_seconds"], 300)

    async def test_configuration_change_after_last_page_prevents_atomic_completion(self):
        await self.enable(delta=True)
        plan = await self.start()
        await self.save(plan, entry())
        await self.operational(poll_interval_seconds=600)
        with self.assertRaises(service.CollectionError) as raised:
            await self.complete(plan)
        self.assertEqual(raised.exception.code, "order_collection_settings_changed")
        self.assertEqual((await self.settings()).cursor_at, CUTOVER)
        self.assertEqual((await self.runs())[0].status, "running")

    async def test_processing_rate_limit_postpones_pending_one_off_without_consuming_or_enabling(self):
        await self.operational(shop_overrides={})
        await self.refresh(None)
        deadline = NOW + timedelta(seconds=900)
        async with self.transaction() as db:
            await db.execute(update(StockShopSettings).where(StockShopSettings.shop_id == self.shops["biketrek"])
                             .values(processing_retry_after_at=deadline))
        self.assertFalse(await worker.cycle())
        self.source.assert_not_awaited()
        row = await self.settings()
        self.assertEqual(row.next_poll_at, deadline)
        self.assertEqual(row.manual_requested_at, NOW)
        self.assertFalse(row.enabled)
        self.assertEqual(row.cursor_at, CUTOVER)
        self.assertEqual(await self.runs(), [])
        with self.assertRaises(service.CollectionError) as raised:
            await self.refresh(1)
        self.assertEqual((raised.exception.code, raised.exception.status), ("order_collection_retry_later", 429))
        self.clock.return_value = deadline
        self.assertTrue(await worker.cycle())
        self.assertFalse((await self.settings()).enabled)

    async def test_processing_rate_limit_during_get_discards_response_and_preserves_wait(self):
        await self.operational(shop_overrides={})
        await self.enable(delta=True)
        deadline = NOW + timedelta(seconds=900)
        async def source(shop, **params):
            async with self.transaction() as db:
                await db.execute(update(StockShopSettings).where(StockShopSettings.shop_id == self.shops["biketrek"])
                                 .values(processing_retry_after_at=deadline))
            return page(entry())
        self.source.side_effect = source
        self.assertTrue(await worker.cycle())
        self.source.assert_awaited_once()
        row = await self.settings()
        self.assertTrue(row.enabled)
        self.assertEqual(row.retry_after_at, deadline)
        self.assertEqual(row.next_poll_at, deadline)
        self.assertEqual(row.last_error, "order_collection_retry_later")
        self.assertEqual(row.cursor_at, CUTOVER)
        self.assertEqual(await self.inbox_rows(), [])
        self.assertFalse(await worker.cycle())

    async def test_cutover_cursor_and_reconciliation_progress_survive_pause_resume_and_revision_guards(self):
        await self.enable()
        original = await self.settings()
        self.assertEqual((original.cursor_at, original.reconcile_cursor_at), (CUTOVER, CUTOVER))
        plan = await self.start()
        await self.complete(plan)
        progress = await self.settings()
        self.assertEqual(progress.reconcile_cursor_at, CUTOVER + timedelta(days=7))
        await self.configure(enabled=False, revision=1)
        with self.assertRaises(service.CollectionError) as raised:
            await self.configure(revision=1)
        self.assertEqual(raised.exception.code, "order_collection_settings_changed")
        await self.configure(revision=2)
        resumed = await self.settings()
        self.assertEqual(resumed.cursor_at, progress.cursor_at)
        self.assertEqual(resumed.reconcile_cursor_at, progress.reconcile_cursor_at)
        self.assertEqual(resumed.reconcile_until_at, progress.reconcile_until_at)
        self.assertEqual(resumed.revision, 3)

    async def test_connection_target_change_pauses_before_network_and_cannot_reuse_old_inbox_scope(self):
        await self.enable()
        self.config["upgates_api_base_url"] = "https://different-test.invalid/api/v2"
        self.assertFalse(await worker.cycle())
        self.source.assert_not_awaited()
        stopped = await self.settings()
        self.assertFalse(stopped.enabled)
        self.assertEqual(stopped.last_error, "order_collection_target_changed")
        self.assertEqual(stopped.cursor_at, CUTOVER)
        with self.assertRaises(service.CollectionError) as raised:
            await self.configure(revision=stopped.revision)
        self.assertEqual(raised.exception.code, "order_collection_target_changed")

    async def test_changed_shop_platform_disables_collection_before_any_upstream_read(self):
        await self.enable()
        async with self.transaction() as db:
            await db.execute(update(Shop).where(Shop.id == self.shops["biketrek"]).values(platform="other"))
        self.assertFalse(await worker.cycle())
        self.source.assert_not_awaited()
        state = await self.settings()
        self.assertFalse(state.enabled)
        self.assertEqual(state.last_error, "order_collection_not_configured")
        self.assertEqual(state.cursor_at, CUTOVER)

    async def test_durable_inbox_deduplicates_and_ignores_older_observations_but_retains_equal_time_conflicts(self):
        await self.enable()
        plan = await self.start()
        observed = entry()
        await self.save(plan, observed)
        first = (await self.inbox_rows())[0]
        self.clock.return_value = NOW + timedelta(seconds=1)
        await self.save(plan, observed)
        duplicate = (await self.inbox_rows())[0]
        self.assertEqual(duplicate.id, first.id)
        self.assertEqual(duplicate.observed_at, first.observed_at)
        self.assertGreater(duplicate.last_seen_at, first.last_seen_at)
        await self.save(plan, {**observed, "updated_at": (NOW - timedelta(minutes=2)).isoformat(), "status_id": 8})
        self.assertEqual((await self.inbox_rows())[0].status_id, 1)
        await self.save(plan, {**observed, "status_id": 8})
        conflict = (await self.inbox_rows())[0]
        self.assertEqual(conflict.status_id, 1)
        self.assertEqual(conflict.review_reason, "order_collection_source_conflict")
        await self.save(plan, {**observed, "updated_at": NOW.isoformat(), "status_id": 8})
        latest = (await self.inbox_rows())[0]
        self.assertEqual(latest.status_id, 8)
        self.assertEqual(latest.review_reason, "order_collection_source_conflict")
        self.assertEqual(len(await self.inbox_rows()), 1)

    async def test_uuid_number_and_creation_identity_conflicts_never_replace_existing_inbox_identity(self):
        await self.enable()
        plan = await self.start()
        first, second = entry("FIRST"), entry("SECOND")
        await self.save(plan, first, second)
        await self.save(plan, {**first, "order_number": "SECOND"})
        rows = await self.inbox_rows()
        self.assertEqual([(row.source_uuid, row.order_number) for row in rows],
                         [(first["uuid"], "FIRST"), (second["uuid"], "SECOND")])
        self.assertTrue(all(row.review_reason == "order_collection_identity_conflict" for row in rows))
        await self.save(plan, {**first, "uuid": str(uuid4())})
        await self.save(plan, {**first, "created_at": CUTOVER.isoformat(), "updated_at": NOW.isoformat()})
        self.assertEqual(len(await self.inbox_rows()), 2)
        self.assertEqual((await self.inbox_rows())[0].created_at, datetime.fromisoformat(first["created_at"]))

    async def test_soft_deleted_inbox_observation_is_review_only_even_for_reserved_local_order(self):
        await self.enable()
        plan = await self.start()
        observed = entry("LEDGER-01", uuid=self.existing_uuid)
        await self.save(plan, observed)
        await self.save(plan, {**observed, "updated_at": NOW.isoformat(), "deleted": True, "status_id": 2})
        async with self.sessions() as db:
            response = await service.inbox(db, "biketrek")
        row = response["entries"][0]
        self.assertTrue(row["deleted"])
        self.assertEqual(row["review_reason"], "order_collection_deleted")
        self.assertEqual(row["stock_state"], "reserved")
        self.assertEqual(row["stock_updated_at"], CUTOVER.isoformat())
        self.assertNotIn("observation_hash", row)
        self.assertNotIn("products", row)

    async def test_cursor_advances_only_after_all_active_and_deleted_pages_commit(self):
        await self.enable(delta=True)
        active1, active2, deleted = entry("ACTIVE-1"), entry("ACTIVE-2"), entry("DELETED", deleted=True)
        calls = []
        async def source(shop, **params):
            self.assertEqual((await self.settings()).cursor_at, CUTOVER)
            calls.append((params["deleted"], params["page"]))
            self.assertEqual(params["expected_target_fingerprint"], service.target_fingerprint("biketrek"))
            if params["deleted"]:
                self.assertEqual(len(await self.inbox_rows()), 2)
                return page(deleted)
            return page(active1 if params["page"] == 1 else active2, number=params["page"], pages=2, total=2)
        self.source.side_effect = source
        self.assertTrue(await worker.cycle())
        self.assertEqual(calls, [(False, 1), (False, 2), (True, 1)])
        self.assertEqual((await self.settings()).cursor_at, NOW)
        run = (await self.runs())[0]
        self.assertEqual((run.status, run.pages, run.observed_count), ("completed", 3, 3))

    async def test_failed_deleted_pass_keeps_cursor_and_partial_inbox_then_retry_deduplicates(self):
        await self.enable(delta=True)
        active, deleted = entry(), entry("DELETED", deleted=True)
        self.source.side_effect = [page(active), CollectionSourceError("order_collection_source_unavailable")]
        await worker.cycle()
        failed = await self.settings()
        self.assertEqual(failed.cursor_at, CUTOVER)
        self.assertEqual(len(await self.inbox_rows()), 1)
        self.assertEqual((await self.runs())[0].status, "failed")
        self.clock.return_value = failed.retry_after_at
        self.source.side_effect = [page(active), page(deleted)]
        await worker.cycle()
        self.assertEqual(len(await self.inbox_rows()), 2)
        self.assertEqual((await self.settings()).cursor_at, failed.retry_after_at)
        self.assertEqual((await self.settings()).failure_count, 0)
        self.assertEqual([run.status for run in await self.runs()], ["failed", "completed"])

    async def test_unstable_page_metadata_keeps_partial_rows_without_checkpoint(self):
        await self.enable(delta=True)
        self.source.side_effect = [page(entry("FIRST"), number=1, pages=2, total=2),
                                   page(entry("SECOND"), number=2, pages=2, total=3)]
        await worker.cycle()
        self.assertEqual((await self.settings()).cursor_at, CUTOVER)
        self.assertEqual((await self.settings()).last_error, "order_collection_unstable_scan")
        self.assertEqual(len(await self.inbox_rows()), 1)

    async def test_page_limit_failure_does_not_attempt_entire_backlog_or_advance_cursor(self):
        await self.operational(max_pages_per_pass=2)
        await self.enable(delta=True)
        self.source.return_value = page(entry(), pages=3, total=3)
        await worker.cycle()
        self.source.assert_awaited_once()
        self.assertEqual((await self.settings()).last_error, "order_collection_backlog_limit")
        self.assertEqual((await self.settings()).cursor_at, CUTOVER)
        self.assertEqual(await self.inbox_rows(), [])

    async def test_duplicate_cross_page_identity_or_backward_update_time_is_not_a_complete_scan(self):
        await self.enable(delta=True)
        first = entry()
        for second in (first, entry("OLDER", updated_at=(NOW - timedelta(minutes=2)).isoformat())):
            self.source.side_effect = [page(first, number=1, pages=2, total=2), page(second, number=2, pages=2, total=2)]
            await worker.cycle()
            state = await self.settings()
            self.assertEqual(state.last_error, "order_collection_unstable_scan")
            self.assertEqual(state.cursor_at, CUTOVER)
            self.clock.return_value = state.retry_after_at

    async def test_429_backoff_survives_refresh_pause_resume_and_restart(self):
        await self.enable(delta=True)
        self.source.side_effect = CollectionSourceError("order_collection_rate_limited", 429, retry_after=900)
        await worker.cycle()
        state = await self.settings()
        self.assertEqual(state.retry_after_at, NOW + timedelta(seconds=900))
        with self.assertRaises(service.CollectionError) as raised:
            await self.refresh(state.revision)
        self.assertEqual(raised.exception.code, "order_collection_retry_later")
        await self.configure(enabled=False, revision=1)
        await self.configure(revision=2)
        self.assertEqual((await self.settings()).next_poll_at, state.retry_after_at)
        self.assertEqual((await self.settings()).last_error, "order_collection_rate_limited")
        async with self.sessions() as db:
            config = await stock_settings.effective(db, self.shops["biketrek"])
        self.assertEqual(config["retry_after_at"], state.retry_after_at)
        self.source.reset_mock()
        self.clock.return_value = NOW + timedelta(seconds=899)
        self.assertFalse(await worker.cycle())
        self.source.assert_not_awaited()
        self.clock.return_value = state.retry_after_at
        self.source.side_effect = None
        self.source.return_value = page()
        self.assertTrue(await worker.cycle())
        self.assertIsNone((await self.settings()).retry_after_at)

    async def test_upstream_auth_error_disables_collection_until_explicit_operator_request(self):
        await self.enable()
        self.source.side_effect = CollectionSourceError("order_collection_upgates_access")
        await worker.cycle()
        state = await self.settings()
        self.assertFalse(state.enabled)
        self.assertEqual(state.revision, 2)
        self.assertEqual(state.cursor_at, CUTOVER)
        self.clock.return_value = NOW + timedelta(days=2)
        self.source.reset_mock()
        self.assertFalse(await worker.cycle())
        self.source.assert_not_awaited()
        response = await self.refresh(state.revision)
        self.assertFalse(response["collector"]["enabled"])
        self.assertTrue(response["collector"]["manual_pending"])
        self.source.side_effect = None
        self.assertTrue(await worker.cycle())
        self.assertFalse((await self.settings()).enabled)

    async def test_pause_during_fetch_discards_response_without_overriding_new_operator_decision(self):
        await self.enable(delta=True)
        async def source(shop, **params):
            await self.configure(enabled=False, revision=1)
            return page(entry())
        self.source.side_effect = source
        await worker.cycle()
        state = await self.settings()
        self.assertFalse(state.enabled)
        self.assertEqual(state.revision, 2)
        self.assertEqual(state.failure_count, 0)
        self.assertIsNone(state.last_error)
        self.assertEqual(state.cursor_at, CUTOVER)
        self.assertEqual(await self.inbox_rows(), [])
        self.assertEqual((await self.runs())[0].error, "order_collection_interrupted")

    async def test_target_change_during_fetch_discards_response_and_pauses_collection(self):
        await self.enable(delta=True)
        async def source(shop, **params):
            self.config["upgates_login"] = "different-operator"
            return page(entry())
        self.source.side_effect = source
        await worker.cycle()
        self.assertFalse((await self.settings()).enabled)
        self.assertEqual((await self.settings()).last_error, "order_collection_target_changed")
        self.assertEqual(await self.inbox_rows(), [])
        self.assertEqual((await self.settings()).cursor_at, CUTOVER)

    async def test_process_restart_recovers_running_attempt_and_repolls_without_duplicate_inbox(self):
        await self.enable(delta=True)
        observed = entry()
        interrupted = await self.start()
        await self.save(interrupted, observed)
        self.assertFalse(await worker.cycle())
        self.source.assert_not_awaited()
        failed = await self.settings()
        self.assertEqual(failed.last_error, "order_collection_interrupted")
        self.assertEqual(failed.cursor_at, CUTOVER)
        self.clock.return_value = failed.retry_after_at
        self.source.side_effect = [page(observed), page()]
        await worker.cycle()
        self.assertEqual(len(await self.inbox_rows()), 1)
        self.assertEqual([run.status for run in await self.runs()], ["failed", "completed"])
        self.source.reset_mock()
        self.assertFalse(await worker.cycle())
        self.source.assert_not_awaited()

    async def test_two_worker_replicas_use_one_session_lock_across_page_transactions(self):
        await self.enable(delta=True)
        entered, release = asyncio.Event(), asyncio.Event()
        async def source(shop, **params):
            if not params["deleted"]:
                entered.set()
                await release.wait()
            return page()
        self.source.side_effect = source
        first = asyncio.create_task(worker.cycle())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            self.assertFalse(await asyncio.wait_for(worker.cycle(), 5))
            self.assertEqual(self.source.await_count, 1)
            self.assertEqual(len(await self.runs()), 1)
        finally:
            release.set()
            await asyncio.wait_for(first, 5)
        self.assertEqual((await self.runs())[0].status, "completed")
        self.assertEqual(self.source.await_count, 2)

    async def test_operator_pause_racing_new_inbox_insert_finishes_without_foreign_key_lock_deadlock(self):
        await self.enable(delta=True)
        plan = await self.start()
        async with self.sessions() as holder:
            await holder.execute(select(Shop).where(Shop.id == self.shops["biketrek"]).with_for_update())
            pause = asyncio.create_task(self.configure(enabled=False, revision=1))
            await asyncio.sleep(0.05)
            save = asyncio.create_task(self.save(plan, entry()))
            try:
                await asyncio.sleep(0.05)
                self.assertFalse(pause.done())
                self.assertFalse(save.done())
            finally:
                await holder.rollback()
            results = await asyncio.wait_for(asyncio.gather(pause, save, return_exceptions=True), 5)
        self.assertIsInstance(results[0], dict)
        # Either serialization order is safe: completed page, or interrupted
        # page after the newer pause. Neither can lose the operator decision.
        self.assertTrue(results[1] is None or isinstance(results[1], service.CollectionError), results)
        if isinstance(results[1], service.CollectionError):
            self.assertEqual(results[1].code, "order_collection_interrupted")
        state = await self.settings()
        self.assertFalse(state.enabled)
        self.assertEqual(state.revision, 2)
        self.assertEqual(state.cursor_at, CUTOVER)

    async def test_reconciliation_scans_weekly_creation_windows_alternating_with_delta(self):
        await self.enable()
        for index in range(5):
            self.clock.return_value = NOW + timedelta(seconds=OperationalValues().poll_interval_seconds * index)
            self.assertTrue(await worker.cycle())
        runs = await self.runs()
        self.assertEqual([run.mode for run in runs], ["reconcile", "delta", "reconcile", "delta", "reconcile"])
        reconciliations = [run for run in runs if run.mode == "reconcile"]
        self.assertEqual([(run.from_at, run.until_at) for run in reconciliations], [
            (CUTOVER, CUTOVER + timedelta(days=7)),
            (CUTOVER + timedelta(days=7), CUTOVER + timedelta(days=14)),
            (CUTOVER + timedelta(days=14), NOW),
        ])
        state = await self.settings()
        self.assertEqual(state.reconcile_cursor_at, CUTOVER)
        self.assertIsNone(state.reconcile_until_at)
        self.assertEqual(state.last_reconciled_at, runs[-1].started_at)
        self.assertEqual(state.cursor_at, runs[-2].started_at)
        self.clock.return_value += timedelta(seconds=OperationalValues().poll_interval_seconds)
        await worker.cycle()
        self.assertEqual((await self.runs())[-1].mode, "delta")

    async def test_migration_rerun_preserves_cursor_run_and_inbox(self):
        await self.enable(delta=True)
        self.source.side_effect = [page(entry()), page()]
        await worker.cycle()
        before = await self.settings()
        rows = await self.inbox_rows()
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'SET search_path TO "{self.schema}"')
            await connection.execute((self.sql_root / "008_order_collection.sql").read_text())
            await connection.execute((self.sql_root / "009_stock_automation.sql").read_text())
        finally:
            await connection.close()
        self.assertEqual((await self.settings()).cursor_at, before.cursor_at)
        self.assertEqual([row.id for row in await self.inbox_rows()], [row.id for row in rows])
        self.assertEqual(len(await self.runs()), 1)
