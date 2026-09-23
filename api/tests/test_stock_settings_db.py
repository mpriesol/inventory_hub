"""Revision, inheritance, activation and migration durability in guarded PostgreSQL."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4
import asyncpg
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool
from inventory_hub.db_models import Shop, Warehouse
from inventory_hub.order_stock_models import OrderStockPolicy
from inventory_hub.order_collection_models import OrderCollectionSettings
from inventory_hub.stock_settings_types import ShopSettingsInput, WarehouseSettingsInput, OperationalValues
from inventory_hub.services import stock_settings as service, order_collection

TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")
NOW = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


@unittest.skipUnless(TEST_URL, "Set CATALOG_TEST_DATABASE_URL to isolated localhost *_catalog_test DB")
class StockSettingsDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("Settings tests require a dedicated localhost *_catalog_test database")
        self.schema = "stock_settings_test_" + uuid4().hex
        self.sql_root = Path(__file__).resolve().parents[2] / "infra" / "db-init"
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            await connection.execute((self.sql_root / "001_schema.sql").read_text())
            await connection.execute("CREATE TYPE payment_status AS ENUM ('unpaid', 'partial', 'paid')")
            for filename in ("002_invoice_management.sql", "007_order_stock.sql", "008_order_collection.sql", "009_stock_automation.sql"):
                await connection.execute((self.sql_root / filename).read_text())
        finally:
            await connection.close()
        self.engine = create_async_engine(TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1), poolclass=NullPool,
            connect_args={"server_settings": {"search_path": self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        self.clock_patch = patch.object(service, "now", return_value=NOW)
        self.clock = self.clock_patch.start()
        self.target_patch = patch.object(order_collection, "target_fingerprint", return_value="a" * 64)
        self.target = self.target_patch.start()
        async with self.transaction() as db:
            shops = (await db.scalars(select(Shop))).all()
            self.shops = {shop.code: shop.id for shop in shops}
            warehouse = Warehouse(code="settings-central", name="Settings central")
            db.add(warehouse)
            await db.flush()
            self.warehouse_id = warehouse.id
            for shop in shops:
                db.add(OrderStockPolicy(shop_id=shop.id, warehouse_id=warehouse.id, starts_at=NOW - timedelta(days=1),
                    revision=1, status_actions={"1": "reserve", "8": "issue"}, status_hash="a" * 64, statuses=[]))
                db.add(OrderCollectionSettings(shop_id=shop.id, enabled=False, revision=1, target_fingerprint="a" * 64,
                    cursor_at=NOW, reconcile_cursor_at=NOW, next_poll_at=NOW))

    async def asyncTearDown(self):
        self.target_patch.stop()
        self.clock_patch.stop()
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

    async def shop(self, **changes):
        payload = dict(shop_code="biketrek", expected_revision=0, expected_warehouse_revision=0, overrides={}, mode="manual", confirmed=True)
        async with self.transaction() as db:
            return await service.configure_shop(db, ShopSettingsInput(**{**payload, **changes}))

    async def warehouse(self, **changes):
        payload = dict(warehouse_code="settings-central", expected_revision=0, values=OperationalValues(), processing_paused=False, confirmed=True)
        async with self.transaction() as db:
            return await service.configure_warehouse(db, WarehouseSettingsInput(**{**payload, **changes}))

    async def effective(self, code="biketrek"):
        async with self.transaction() as db:
            return await service.effective(db, self.shops[code])

    async def test_defaults_and_inheritance_never_inherit_automatic_authority(self):
        initial = await self.effective()
        self.assertEqual(initial["mode"], "manual")
        self.assertEqual(initial["values"]["poll_interval_seconds"], 300)
        await self.warehouse(values={"poll_interval_seconds": 600})
        await self.shop(expected_warehouse_revision=1, overrides={"poll_interval_seconds": 120})
        own, other = await self.effective(), await self.effective("xtrek")
        self.assertEqual((own["values"]["poll_interval_seconds"], other["values"]["poll_interval_seconds"]), (120, 600))
        self.assertEqual(own["sources"]["poll_interval_seconds"], "shop")
        self.assertEqual(other["mode"], "manual")
        await self.shop(expected_revision=1, expected_warehouse_revision=1, overrides={})
        self.assertEqual((await self.effective())["values"]["poll_interval_seconds"], 600)

    async def test_stale_shop_or_warehouse_revision_cannot_overwrite_new_decision(self):
        await self.warehouse(processing_paused=True)
        with self.assertRaises(service.SettingsError):
            await self.shop(mode="reserve")
        await self.shop(expected_warehouse_revision=1, mode="reserve")
        with self.assertRaises(service.SettingsError):
            await self.shop(expected_warehouse_revision=1, mode="manual")
        self.assertEqual((await self.effective())["mode"], "reserve")
        self.assertTrue((await self.effective())["processing_paused"])

    async def test_activation_cutovers_and_fulfillment_confirmation_are_explicit(self):
        first = await self.shop(mode="reserve")
        self.assertEqual(first["effective"]["automation_starts_at"], NOW)
        later = NOW + timedelta(hours=1)
        self.clock.return_value = later
        with self.assertRaises(service.SettingsError) as raised:
            await self.shop(expected_revision=1, mode="fulfill")
        self.assertEqual(raised.exception.code, "stock_settings_fulfillment_confirmation_required")
        full = await self.shop(expected_revision=1, mode="fulfill", fulfillment_confirmed=True)
        self.assertEqual(full["effective"]["automation_starts_at"], NOW)
        self.assertEqual(full["effective"]["issue_starts_at"], later)
        self.clock.return_value += timedelta(hours=1)
        edited = await self.shop(expected_revision=2, mode="fulfill", fulfillment_confirmed=True, overrides={"poll_interval_seconds": 60})
        self.assertEqual(edited["effective"]["issue_starts_at"], later)
        await self.shop(expected_revision=3, mode="manual")
        reactivated = await self.shop(expected_revision=4, mode="reserve")
        self.assertEqual(reactivated["effective"]["automation_starts_at"], self.clock.return_value)
        self.assertIsNone(reactivated["effective"]["issue_starts_at"])

    async def test_missing_collection_or_changed_target_never_grants_authority(self):
        self.target.return_value = None
        with self.assertRaises(service.SettingsError):
            await self.shop(mode="reserve")
        self.target.return_value = "b" * 64
        with self.assertRaises(service.SettingsError) as raised:
            await self.shop(mode="reserve")
        self.assertEqual(raised.exception.code, "stock_settings_target_changed")
        self.assertEqual((await self.effective())["mode"], "manual")

    async def test_inherited_retry_bounds_validated_on_both_write_paths(self):
        await self.shop(overrides={"retry_max_seconds": 300})
        with self.assertRaises(service.SettingsError) as raised:
            await self.warehouse(values={"retry_base_seconds": 600})
        self.assertEqual(raised.exception.code, "stock_settings_inherited_values_conflict")
        self.assertEqual((await self.effective())["warehouse_revision"], 0)
        await self.shop(expected_revision=1, overrides={})
        await self.warehouse(values={"retry_base_seconds": 600})
        with self.assertRaises(service.SettingsError):
            await self.shop(expected_revision=2, expected_warehouse_revision=1, overrides={"retry_max_seconds": 300})

    async def test_pause_changes_effective_hash_and_migration_rerun_preserves_authority(self):
        await self.shop(mode="fulfill", fulfillment_confirmed=True)
        before = await self.effective()
        await self.warehouse(processing_paused=True)
        paused = await self.effective()
        self.assertNotEqual(before["configuration_hash"], paused["configuration_hash"])
        self.assertEqual(before["issue_starts_at"], paused["issue_starts_at"])
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'SET search_path TO "{self.schema}"')
            await connection.execute((self.sql_root / "009_stock_automation.sql").read_text())
        finally:
            await connection.close()
        self.assertEqual((await self.effective())["configuration_hash"], paused["configuration_hash"])

    async def test_effective_readiness_exposes_policy_and_target_blocks(self):
        await self.shop(mode="reserve")
        self.assertTrue((await self.effective())["processing_ready"])
        async with self.transaction() as db:
            policy = await db.get(OrderStockPolicy, self.shops["biketrek"])
            policy.revision += 1
        blocked = await self.effective()
        self.assertFalse(blocked["processing_ready"])
        self.assertEqual(blocked["processing_error"], "order_processing_policy_changed")
        await self.shop(expected_revision=1, mode="reserve")
        self.target.return_value = "b" * 64
        self.assertEqual((await self.effective())["processing_error"], "order_processing_target_changed")
