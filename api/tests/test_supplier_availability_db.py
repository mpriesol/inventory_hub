"""Atomic supplier observations in a disposable PostgreSQL namespace only."""
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub import config_io
from inventory_hub.db_models import FeedRunStatus, Product, SupplierFeedItemRaw, SupplierFeedRun, SupplierProduct
from inventory_hub.db_models_ext import ProductSupplySource
from inventory_hub.services import supplier_availability as service
from inventory_hub.services.supplier_availability_source import AvailabilityError, observation
from inventory_hub.supplier_availability_models import SupplierAvailabilityObservation, SupplierAvailabilitySettings
from inventory_hub.supplier_availability_types import SupplierAvailabilityInput, SupplierAvailabilityRunInput

TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")


@unittest.skipUnless(TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class SupplierAvailabilityDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("Supplier tests require a dedicated localhost *_catalog_test database")
        self.schema = "availability_test_" + uuid4().hex
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            root = Path(__file__).resolve().parents[2] / "infra" / "db-init"
            for name in ("001_schema.sql", "014_supplier_availability.sql", "014_supplier_availability.sql"):
                await connection.execute((root / name).read_text())
        finally:
            await connection.close()
        self.engine = create_async_engine(TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1),
            poolclass=NullPool, connect_args={"server_settings": {"search_path": self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        self.folder = TemporaryDirectory()
        self.patch = patch.object(config_io, "DATA_ROOT", Path(self.folder.name))
        self.patch.start()
        self.config = {"name": "Test supplier", "feeds": {"sources": {
            "stock": {"mode": "local", "local_path": "stock.xml"},
            "products": {"mode": "local", "local_path": "products.xml"}}},
            "adapter_settings": {"availability": {"orderable": "do 7 dní"}}}
        path = config_io.supplier_path("paul-lange")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(self.config))
        async with self.transaction() as db:
            await service.configure(db, "paul-lange", SupplierAvailabilityInput(expected_revision=0, feed_key="stock"))
            self.supplier_id = await db.scalar(select(SupplierAvailabilitySettings.supplier_id))
            self.products = []
            for index in (1, 2):
                source = SupplierProduct(supplier_id=self.supplier_id, supplier_sku=f"SUP-{index}", name=f"Source {index}")
                db.add(source)
                await db.flush()
                product = Product(sku=f"LOCAL-{index}", name=f"Product {index}", source_supplier_product_id=source.id)
                db.add(product)
                await db.flush()
                self.products.append(product.id)
                if index == 1:
                    db.add(ProductSupplySource(product_id=product.id, supplier_product_id=source.id, is_primary=True))

    async def asyncTearDown(self):
        self.patch.stop()
        self.folder.cleanup()
        await self.engine.dispose()
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            await connection.close()

    @asynccontextmanager
    async def transaction(self):
        async with self.sessions() as db:
            async with db.begin():
                yield db

    async def start(self, revision=1):
        async with self.transaction() as db:
            await service.request_run(db, "paul-lange", SupplierAvailabilityRunInput(expected_revision=revision))
        async with self.transaction() as db:
            return await service.start_run(db, self.supplier_id)

    async def accept(self, records=None, revision=1):
        plan = await self.start(revision)
        async with self.transaction() as db:
            await service.accept_run(db, plan, records or [observation("SUP-1", "6+"), observation("SUP-2", "0")], 120)
        return plan

    async def test_manual_run_disabled_automation_accepts_only_supplier_facts(self):
        async with self.transaction() as db:
            self.assertIsNone(await service.start_run(db, self.supplier_id))
        plan = await self.accept()
        async with self.transaction() as db:
            settings = await db.get(SupplierAvailabilitySettings, self.supplier_id)
            self.assertFalse(settings.enabled)
            self.assertIsNone(settings.running_run_id)
            self.assertEqual(settings.last_item_count, 2)
            self.assertEqual((await db.get(SupplierFeedRun, plan["run_id"])).status, FeedRunStatus.completed)
            self.assertEqual(await db.scalar(select(func.count()).select_from(SupplierFeedItemRaw)), 2)
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM stock_movements")), 0)
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM stock_balances")), 0)
            projected = await service.project(db, self.products, plan["observed_at"] + timedelta(seconds=1))
            self.assertEqual(projected[self.products[0]]["label"], "do 7 dní")
            self.assertEqual(projected[self.products[0]]["quantity_kind"], "minimum")
            self.assertEqual(projected[self.products[1]]["label"], "overíme")
            self.assertTrue(projected[self.products[1]]["orderable"])
            stale = await service.project(db, self.products, plan["observed_at"] + timedelta(seconds=21600))
            self.assertIsNone(stale[self.products[0]]["available"])

    async def test_shrunken_feed_and_failed_fetch_keep_last_success(self):
        first = await self.accept()
        second = await self.start()
        with self.assertRaises(AvailabilityError):
            async with self.transaction() as db:
                await service.accept_run(db, second, [observation("SUP-1", "0")])
        async with self.transaction() as db:
            await service.fail_run(db, self.supplier_id, second["run_id"], "supplier_availability_incomplete_feed")
        async with self.transaction() as db:
            row = await db.get(SupplierAvailabilitySettings, self.supplier_id)
            self.assertEqual(row.last_success_at, first["observed_at"])
            self.assertEqual(row.last_item_count, 2)
            observation_row = await db.get(SupplierAvailabilityObservation, (self.supplier_id, "SUP-1"))
            self.assertEqual(observation_row.run_id, first["run_id"])
            self.assertTrue(observation_row.available)

    async def test_settings_or_credentials_changed_during_download_discard_result(self):
        plan = await self.start()
        async with self.transaction() as db:
            await service.configure(db, "paul-lange", SupplierAvailabilityInput(expected_revision=1, feed_key="stock", interval_seconds=7200))
        with self.assertRaises(AvailabilityError):
            async with self.transaction() as db:
                await service.accept_run(db, plan, [observation("SUP-1", "1")])
        async with self.transaction() as db:
            await service.fail_run(db, self.supplier_id, plan["run_id"], "supplier_availability_changed")
        second = await self.start(revision=2)
        self.config["name"] = "Changed while downloading"
        config_io.supplier_path("paul-lange").write_text(json.dumps(self.config))
        with self.assertRaises(AvailabilityError):
            async with self.transaction() as db:
                await service.accept_run(db, second, [observation("SUP-1", "1")])
        async with self.transaction() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(SupplierAvailabilityObservation)), 0)

    async def test_second_claim_and_stale_settings_are_rejected(self):
        await self.start()
        async with self.transaction() as db:
            self.assertIsNone(await service.start_run(db, self.supplier_id))
        with self.assertRaises(AvailabilityError):
            async with self.transaction() as db:
                await service.configure(db, "paul-lange", SupplierAvailabilityInput(expected_revision=0, feed_key="stock"))

    async def test_manual_request_during_running_pass_is_preserved(self):
        plan = await self.start()
        async with self.transaction() as db:
            await service.request_run(db, "paul-lange", SupplierAvailabilityRunInput(expected_revision=1))
        async with self.transaction() as db:
            await service.accept_run(db, plan, [observation("SUP-1", "1")])
        async with self.transaction() as db:
            second = await service.start_run(db, self.supplier_id)
            self.assertIsNotNone(second)
            self.assertNotEqual(second["run_id"], plan["run_id"])

    async def test_explicitly_disabled_source_does_not_use_legacy_fallback(self):
        plan = await self.accept()
        async with self.transaction() as db:
            source = await db.scalar(select(ProductSupplySource).where(ProductSupplySource.product_id == self.products[0]))
            source.orderable = False
        async with self.transaction() as db:
            projected = await service.project(db, self.products, plan["observed_at"] + timedelta(seconds=1))
            self.assertEqual(projected[self.products[0]]["label"], "overíme")
            self.assertIsNone(projected[self.products[0]]["source"])
