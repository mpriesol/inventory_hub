"""Atomic supplier observations in a disposable PostgreSQL namespace only."""
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub import config_io
from inventory_hub.db_models import FeedRunStatus, IdentifierType, Product, ProductIdentifier, Supplier, SupplierFeedItemRaw, SupplierFeedRun, SupplierProduct
from inventory_hub.db_models_ext import ProductSupplySource
from inventory_hub.services import supplier_availability as service
from inventory_hub.services import supplier_links, catalog
from inventory_hub.services import supplier_availability_b2b as b2b
from inventory_hub.sloger_probe_types import SlogerObservation
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

    async def test_linked_source_without_first_run_is_not_reported_as_missing_sku(self):
        async with self.transaction() as db:
            value = (await service.project(db, self.products))[self.products[0]]
            self.assertEqual((value["status"], value["action"]), ("not_loaded", "refresh"))
            self.assertEqual((value["supplier"], value["supplier_sku"], value["feed_key"]), ("paul-lange", "SUP-1", "stock"))
            self.assertIsNone(value["last_success_at"])
            self.assertEqual(value["availability_state"], "unknown")

    async def test_unconfigured_link_preserves_supplier_context(self):
        async with self.transaction() as db:
            await db.delete(await db.get(SupplierAvailabilitySettings, self.supplier_id))
        async with self.transaction() as db:
            value = (await service.project(db, self.products))[self.products[0]]
            self.assertEqual((value["status"], value["action"]), ("unconfigured", "configure"))
            self.assertEqual(value["supplier_sku"], "SUP-1")
            self.assertIsNone(value["feed_key"])

    async def test_successful_feed_missing_exact_sku_has_distinct_diagnostic(self):
        plan = await self.accept([observation("SUP-2", "0")])
        async with self.transaction() as db:
            value = await service.source_observation(db, "paul-lange", "SUP-1")
            self.assertEqual(value["status"], "missing_observation")
            self.assertEqual(value["last_success_at"], plan["observed_at"].isoformat())
            self.assertIsNone(value["observed_at"])
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM stock_movements")), 0)

    async def test_changing_selected_feed_reports_mismatch_instead_of_missing_sku(self):
        plan = await self.accept()
        async with self.transaction() as db:
            await service.configure(db, "paul-lange", SupplierAvailabilityInput(expected_revision=1, feed_key="products"))
        async with self.transaction() as db:
            value = (await service.project(db, self.products))[self.products[0]]
            self.assertEqual((value["status"], value["action"]), ("feed_mismatch", "check_source"))
            self.assertEqual((value["feed_key"], value["observation_feed_key"]), ("products", "stock"))
            self.assertEqual(value["observed_at"], plan["observed_at"].isoformat())
            self.assertFalse(value["fresh"])
            self.assertIsNone(value["quantity"])

    async def test_raw_supplier_sku_drives_zero_quantity_without_catalog_or_hub_sku_fallback(self):
        async with self.transaction() as db:
            source = await db.scalar(select(SupplierProduct).where(SupplierProduct.supplier_sku == "SUP-1"))
            source.supplier_sku = "103404-281-103"
            source.stock_qty = 99  # Old catalog snapshot is deliberately different.
        plan = await self.accept([observation("103404-281-103", "0"), observation("SUP-2", "1")])
        async with self.transaction() as db:
            value = (await service.project(db, self.products, plan["observed_at"] + timedelta(seconds=1)))[self.products[0]]
            self.assertEqual(value["availability_state"], "unavailable")
            self.assertEqual(value["status"], "fresh")
            self.assertEqual(Decimal(value["quantity"]), Decimal(0))
            self.assertEqual(value["supplier_sku"], "103404-281-103")
            self.assertFalse((await db.get(SupplierAvailabilitySettings, self.supplier_id)).enabled)
            direct = await service.source_observation(db, "paul-lange", "103404-281-103", plan["observed_at"] + timedelta(seconds=1))
            self.assertEqual(direct["availability_state"], "unavailable")
            with self.assertRaises(AvailabilityError):
                await service.source_observation(db, "paul-lange", "NF-103404-281-103")
            with self.assertRaises(AvailabilityError):
                await service.source_observation(db, "paul-lange", "LOCAL-1")

    async def test_configuration_failure_is_not_misreported_as_missing_observation(self):
        await self.accept()
        with patch.object(service.catalog, "supplier_config", side_effect=service.catalog.CatalogError("supplier_not_found", "secret URL", 404)):
            async with self.transaction() as db:
                value = (await service.project(db, self.products))[self.products[0]]
                self.assertEqual((value["status"], value["action"]), ("configuration_error", "configure"))
                self.assertEqual(value["reason"], "supplier_availability_source_missing")
                self.assertNotIn("secret", json.dumps(value))

    async def test_queued_refresh_is_separate_from_availability_and_does_not_invent_quantity(self):
        async with self.transaction() as db:
            await service.request_run(db, "paul-lange", SupplierAvailabilityRunInput(expected_revision=1))
        async with self.transaction() as db:
            value = (await service.project(db, self.products))[self.products[0]]
            self.assertEqual((value["status"], value["refresh_state"], value["action"]), ("not_loaded", "queued", "wait"))
            self.assertIsNone(value["quantity"])
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM stock_movements")), 0)

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

    async def test_shrunken_feed_accepts_valid_rows_preserves_missing_and_warns(self):
        first = await self.accept()
        second = await self.start()
        async with self.transaction() as db:
            await service.accept_run(db, second, [observation("SUP-1", "0")])
        async with self.transaction() as db:
            row = await db.get(SupplierAvailabilitySettings, self.supplier_id)
            self.assertEqual(row.last_success_at, second["observed_at"])
            self.assertEqual(row.last_item_count, 2)  # Never lower the complete-feed baseline.
            updated = await db.get(SupplierAvailabilityObservation, (self.supplier_id, "SUP-1"))
            missing = await db.get(SupplierAvailabilityObservation, (self.supplier_id, "SUP-2"))
            self.assertFalse(updated.available)
            self.assertEqual(updated.run_id, second["run_id"])
            self.assertEqual(missing.run_id, first["run_id"])
            report = await service.latest_run(db, self.supplier_id, "stock")
            self.assertEqual(report["status"], "partial")
            self.assertEqual(report["report"]["warnings"], ["supplier_availability_incomplete_feed"])
        third = await self.start()
        async with self.transaction() as db:
            await service.fail_run(db, self.supplier_id, third["run_id"], "supplier_availability_parse_failed")
        async with self.transaction() as db:
            self.assertEqual((await db.get(SupplierAvailabilitySettings, self.supplier_id)).last_success_at, second["observed_at"])

    async def test_partial_report_pagination_preserves_rejected_expiry_and_all_invalid_run(self):
        from inventory_hub.services.supplier_availability_source import ParsedAvailability
        first = await self.accept()
        issue = {"row": 1, "sku": "SUP-1", "stock": "-1", "external_stock": None,
                 "code": "supplier_availability_invalid_quantity", "kind": "rejected"}
        issues = [dict(issue, row=i+1) for i in range(51)]
        second = await self.start()
        async with self.transaction() as db:
            # A valid SKU resembling the issue hash namespace must not collide.
            await service.accept_run(db, second, ParsedAvailability([observation("issue:1", "2"), observation("SUP-2", "3")], issues))
        async with self.transaction() as db:
            old = await db.get(SupplierAvailabilityObservation, (self.supplier_id, "SUP-1"))
            self.assertEqual(old.run_id, first["run_id"])
            self.assertEqual(old.expires_at, first["observed_at"] + timedelta(seconds=21600))
            projected = await service.project(db, self.products, old.expires_at)
            self.assertIsNone(projected[self.products[0]]["available"])
            report = await service.latest_run(db, self.supplier_id, "stock")
            self.assertEqual((report["report"]["accepted"], report["report"]["rejected"]), (2, 51))
            page = await service.run_failures(db, "paul-lange", second["run_id"], 50)
            self.assertEqual((page["total"], len(page["items"]), page["items"][0]["row"]), (51, 1, 51))
            with self.assertRaises(AvailabilityError):
                await service.run_failures(db, "northfinder", second["run_id"])
            self.assertEqual(await db.scalar(select(func.count()).select_from(SupplierFeedItemRaw).where(SupplierFeedItemRaw.processed.is_(False))), 0)
        third = await self.start()
        async with self.transaction() as db:
            await service.accept_run(db, third, ParsedAvailability([], [issue]))
        async with self.transaction() as db:
            row = await db.get(SupplierAvailabilitySettings, self.supplier_id)
            self.assertEqual(row.last_success_at, second["observed_at"])
            self.assertEqual(row.last_error, "supplier_availability_no_usable_rows")
            report = await service.latest_run(db, self.supplier_id, "stock")
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["report"]["accepted"], 0)
            self.assertIsNone(row.running_run_id)
            self.assertGreater(row.next_run_at, third["observed_at"])

    async def test_northfinder_automatic_worker_repeats_partial_refresh_without_manual_queue(self):
        from inventory_hub import database
        from inventory_hub.services import supplier_availability_worker as worker
        config = {"name": "Northfinder test", "feeds": {"sources": {"stock": {"mode": "local", "local_path": "stock.xml"}}}}
        path = config_io.supplier_path("northfinder")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(config))
        feed = Path(self.folder.name) / "stock.xml"
        feed.write_text('<products><product><variants><variant><reference>A</reference><quantity>2</quantity></variant>'
                        '<variant><reference>B</reference><quantity>-1</quantity></variant></variants></product></products>')
        async with self.transaction() as db:
            await service.configure(db, "northfinder", SupplierAvailabilityInput(expected_revision=0, feed_key="stock", enabled=True))
            supplier_id = await db.scalar(select(Supplier.id).where(Supplier.code == "northfinder"))
            source = SupplierProduct(supplier_id=supplier_id, supplier_sku="A", name="Generic variant")
            db.add(source)
            await db.flush()
            product = Product(sku="EXISTING-SKU", name="Existing product", source_supplier_product_id=source.id)
            db.add(product)
            await db.flush()
            product_id = product.id
        with patch.object(database, "_engine", self.engine), patch.object(worker, "get_session_context", self.transaction), \
             patch.object(catalog, "_download", return_value=feed) as download:
            self.assertTrue(await worker.cycle())
            self.assertFalse(await worker.cycle())
            self.assertEqual(download.call_count, 1)
            async with self.transaction() as db:
                row = await db.get(SupplierAvailabilitySettings, supplier_id)
                self.assertIsNone(row.manual_requested_at)
                self.assertIsNotNone(row.last_success_at)
                self.assertGreater(row.next_run_at, row.last_success_at)
                self.assertEqual((await service.latest_run(db, supplier_id, "stock"))["status"], "partial")
                self.assertEqual((await service.project(db, [product_id]))[product_id]["availability_state"], "available")
                row.next_run_at = service.now() - timedelta(seconds=1)
            feed.write_text(feed.read_text().replace('<quantity>2</quantity>', '<quantity>0</quantity>'))
            self.assertTrue(await worker.cycle())
            self.assertEqual(download.call_count, 2)
        async with self.transaction() as db:
            projection = (await service.project(db, [product_id]))[product_id]
            self.assertEqual((projection["quantity"], projection["availability_state"]), ("0.000", "unavailable"))
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM stock_movements")), 0)
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM stock_balances")), 0)
        from inventory_hub.supplier_availability_diagnostics import snapshot
        async with self.transaction() as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            evidence = await snapshot(db, "northfinder", [])
            self.assertTrue(evidence["read_only"])
            self.assertEqual([run["trigger"] for run in evidence["recent_runs"]], ["scheduled", "scheduled"])
            self.assertEqual(evidence["latest_run"]["report"]["accepted"], 1)

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

    def set_prefix(self):
        self.config["adapter_settings"]["mapping"] = {"postprocess": {"product_code_prefix": "PL-"}}
        config_io.supplier_path("paul-lange").write_text(json.dumps(self.config))

    async def test_downloaded_observation_repairs_exact_sku_without_new_download_or_stock(self):
        self.set_prefix()
        plan = await self.accept([observation("001234", "6+"), observation("1234", "0")])
        async with self.transaction() as db:
            product = Product(sku="PL-001234", name="Existing shop product", supplier_price=99)
            db.add(product)
            await db.flush()
            product_id = product.id
            before = await service.project(db, [product_id])
            self.assertEqual(before[product_id]["status"], "missing_link")
            self.assertIsNone(await db.scalar(select(SupplierProduct).where(SupplierProduct.supplier_sku == "001234")))
            report = await supplier_links.reconcile_supplier_links(db, "paul-lange", product_ids=[product_id])
            self.assertEqual(report["linked"], 1)
            self.assertEqual(report["conflicts"], [])
            again = await supplier_links.reconcile_supplier_links(db, "paul-lange", product_ids=[product_id])
            self.assertEqual((again["linked"], again["existing"]), (0, 1))
            projected = await service.project(db, [product_id], plan["observed_at"] + timedelta(seconds=1))
            self.assertEqual(projected[product_id]["label"], "do 7 dní")
            self.assertEqual(projected[product_id]["quantity"], "6.000")
            self.assertEqual((product.sku, product.supplier_price), ("PL-001234", 99))
            alias = await db.scalar(select(ProductIdentifier).where(ProductIdentifier.product_id == product_id))
            self.assertEqual(alias.value, "001234")
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM stock_movements")), 0)
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM stock_balances")), 0)

    async def test_feed_acceptance_links_canonical_products_automatically(self):
        self.set_prefix()
        async with self.transaction() as db:
            product = Product(sku="PL-001234", name="Existing product")
            db.add(product)
            await db.flush()
            product_id = product.id
        plan = await self.accept([observation("001234", "2")])
        async with self.transaction() as db:
            self.assertIsNotNone(await db.scalar(select(ProductSupplySource).where(ProductSupplySource.product_id == product_id)))
            run = await db.get(SupplierFeedRun, plan["run_id"])
            self.assertEqual(run.error_details["supplier_links"]["linked"], 1)

    async def test_supplier_alias_keeps_existing_canonical_sku_for_another_supplier(self):
        self.set_prefix()
        async with self.transaction() as db:
            other = Supplier(code="sloger", name="Other supplier")
            db.add(other)
            await db.flush()
            source = SupplierProduct(supplier_id=other.id, supplier_sku="001234", name="Other source")
            product = Product(sku="SL-OTHER", supplier_id=other.id, name="One physical product")
            db.add_all([source, product])
            await db.flush()
            product_id = product.id
            db.add(ProductSupplySource(product_id=product.id, supplier_product_id=source.id, is_primary=True))
            db.add(ProductIdentifier(product_id=product.id, supplier_id=self.supplier_id,
                identifier_type=IdentifierType.supplier_sku, value="001234"))
        await self.accept([observation("001234", "2")])
        async with self.transaction() as db:
            self.assertEqual((await db.get(Product, product_id)).sku, "SL-OTHER")
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductSupplySource).where(
                ProductSupplySource.product_id == product_id)), 2)
            self.assertEqual((await service.project(db, [product_id]))[product_id]["source"], "paul-lange")

    async def test_disabled_link_and_inactive_supplier_item_are_never_revived(self):
        self.set_prefix()
        async with self.transaction() as db:
            disabled = SupplierProduct(supplier_id=self.supplier_id, supplier_sku="OFF", name="Disabled source")
            inactive = SupplierProduct(supplier_id=self.supplier_id, supplier_sku="OLD", name="Inactive source", is_active=False)
            first, second = Product(sku="PL-OFF", name="Off"), Product(sku="PL-OLD", name="Old")
            db.add_all([disabled, inactive, first, second])
            await db.flush()
            ids = [first.id, second.id]
            db.add(ProductSupplySource(product_id=first.id, supplier_product_id=disabled.id,
                is_active=False, orderable=False, priority=42))
        await self.accept([observation("OFF", "2"), observation("OLD", "3")])
        async with self.transaction() as db:
            report = await supplier_links.reconcile_supplier_links(db, "paul-lange", product_ids=ids)
            self.assertEqual(report["linked"], 0)
            link = await db.scalar(select(ProductSupplySource).where(ProductSupplySource.product_id == ids[0]))
            self.assertEqual((link.is_active, link.orderable, link.priority), (False, False, 42))
            self.assertIsNone(await db.scalar(select(ProductSupplySource).where(ProductSupplySource.product_id == ids[1])))
            projected = await service.project(db, ids)
            self.assertEqual(projected[ids[0]]["status"], "unavailable_source")

    async def test_conflicting_known_ean_is_reported_and_never_guessed(self):
        self.set_prefix()
        async with self.transaction() as db:
            source = SupplierProduct(supplier_id=self.supplier_id, supplier_sku="001234", name="Catalog item", ean="4006381333931")
            exact = Product(sku="PL-001234", name="Different known barcode")
            barcode_only = Product(sku="OLD-1234", name="Unprefixed existing product")
            db.add_all([source, exact, barcode_only])
            await db.flush()
            ids = [exact.id, barcode_only.id]
            db.add_all([ProductIdentifier(product_id=exact.id, identifier_type=IdentifierType.ean, value="5901234123457"),
                ProductIdentifier(product_id=barcode_only.id, identifier_type=IdentifierType.ean, value="4006381333931")])
        await self.accept([observation("001234", "2")])
        async with self.transaction() as db:
            report = await supplier_links.reconcile_supplier_links(db, "paul-lange", product_ids=ids)
            self.assertEqual(report["linked"], 0)
            self.assertEqual(len(report["conflicts"]), 1)
            projected = await service.project(db, ids)
            self.assertEqual(projected[ids[0]]["status"], "conflict")
            self.assertEqual(projected[ids[1]]["status"], "missing_link")

    async def test_supplier_source_owned_by_other_product_blocks_new_link(self):
        self.set_prefix()
        async with self.transaction() as db:
            source = SupplierProduct(supplier_id=self.supplier_id, supplier_sku="001234", name="Existing source")
            db.add(source)
            await db.flush()
            product = Product(sku="PL-001234", name="Canonical candidate")
            owner = Product(sku="LEGACY", name="Legacy source owner", source_supplier_product_id=source.id)
            db.add_all([product, owner])
            await db.flush()
            report = await supplier_links.reconcile_supplier_links(db, "paul-lange", product_ids=[product.id])
            self.assertEqual(report["conflicts"][0]["reason"], "supplier_source_multiple_products")
            self.assertEqual(report["linked"], 0)

    async def test_exact_case_required_and_repair_does_not_fabricate_missing_supplier_item(self):
        self.set_prefix()
        async with self.transaction() as db:
            db.add(SupplierProduct(supplier_id=self.supplier_id, supplier_sku="ABC", name="Case-sensitive item"))
            exact, lower, missing = Product(sku="PL-ABC", name="Exact"), Product(sku="pl-abc", name="Collision"), Product(sku="PL-NONE", name="Unknown source")
            db.add_all([exact, lower, missing])
            await db.flush()
            report = await supplier_links.reconcile_supplier_links(db, "paul-lange", product_ids=[exact.id, lower.id, missing.id])
            self.assertEqual(report["linked"], 0)
            self.assertEqual(len(report["conflicts"]), 1)
            self.assertEqual(report["skipped_details"][0]["reason"], "supplier_item_missing")
            self.assertIsNone(await db.scalar(select(SupplierProduct).where(SupplierProduct.supplier_sku == "NONE")))

    async def test_invoice_alias_without_offer_preserves_existing_feed_link(self):
        self.set_prefix()
        async with self.transaction() as db:
            db.add(ProductIdentifier(product_id=self.products[0], supplier_id=self.supplier_id,
                identifier_type=IdentifierType.supplier_sku, value="INVOICE-CODE"))
            await db.flush()
            report = await supplier_links.reconcile_supplier_links(db, "paul-lange", product_ids=[self.products[0]])
            self.assertEqual(report["conflicts"], [])
            self.assertEqual((report["existing"], report["skipped"], report["linked"]), (1, 1, 0))
            self.assertEqual(report["skipped_details"][0]["reason"], "supplier_item_missing")
            self.assertIsNone(await db.scalar(select(SupplierProduct.id).where(SupplierProduct.supplier_sku == "INVOICE-CODE")))
            # A real second offer remains an explicit conflict; the alias
            # exception must not replace an operator's configured feed link.
            db.add(SupplierProduct(supplier_id=self.supplier_id, supplier_sku="INVOICE-CODE", name="Another offer"))
            await db.flush()
            report = await supplier_links.reconcile_supplier_links(db, "paul-lange", product_ids=[self.products[0]])
            self.assertEqual(report["conflicts"][0]["reason"], "supplier_link_already_configured")
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductSupplySource)
                .where(ProductSupplySource.product_id == self.products[0])), 1)
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM stock_movements")), 0)

    async def test_repair_cursor_is_bounded_and_preserves_catalog_content_and_freshness(self):
        self.set_prefix()
        async with self.transaction() as db:
            source = SupplierProduct(supplier_id=self.supplier_id, supplier_sku="001234", name="Real catalog title",
                purchase_price=17, attributes={"catalog": {"description": "Keep"}}, stock_qty=99)
            first, second = Product(sku="PL-001234", name="First"), Product(sku="PL-OTHER", name="Second")
            db.add_all([source, first, second])
            await db.flush()
            ids, source_id = [first.id, second.id], source.id
        plan = await self.accept([observation("001234", "2"), observation("OTHER", "0")])
        async with self.transaction() as db:
            first = await supplier_links.reconcile_supplier_links(db, "paul-lange", limit=1, product_ids=ids)
            self.assertEqual(first["next_after_product_id"], ids[0])
            second = await supplier_links.reconcile_supplier_links(db, "paul-lange", limit=1, product_ids=ids,
                after_product_id=first["next_after_product_id"])
            self.assertIsNone(second["next_after_product_id"])
            source = await db.get(SupplierProduct, source_id)
            self.assertEqual((source.name, source.purchase_price, source.stock_qty), ("Real catalog title", 17, 99))
            self.assertEqual(source.attributes, {"catalog": {"description": "Keep"}})
            old = await service.project(db, ids, plan["observed_at"] + timedelta(seconds=21601))
            self.assertEqual(old[ids[0]]["status"], "stale")

    async def test_unrelated_repair_does_not_lock_unused_new_supplier_prefix(self):
        config_io.save_supplier("new-supplier", {"product_code_prefix": "NEW-"})
        async with self.transaction() as db:
            db.add(Supplier(code="new-supplier", name="Unused supplier"))
            await db.flush()
            report = await supplier_links.reconcile_supplier_links(db, "new-supplier", product_ids=self.products)
            self.assertEqual(report["linked"], 0)
        self.assertFalse(config_io.supplier_prefix_status("new-supplier")["product_prefix_locked"])
        config_io.save_supplier("new-supplier", {"product_code_prefix": "NEWER-"})

    async def sloger_fixture(self, count=12):
        config_io.save_supplier("sloger", {"name": "Sloger", "adapter_settings": {
            "availability": {"orderable": "do 2-4 dní", "unknown": "overíme"}}})
        async with self.transaction() as db:
            await service.configure(db, "sloger", SupplierAvailabilityInput(expected_revision=0, feed_key="b2b"))
            supplier_id = await db.scalar(select(Supplier.id).where(Supplier.code == "sloger"))
            ids = []
            for index in range(count + 1):
                source = SupplierProduct(supplier_id=supplier_id, supplier_sku=f"SLG-{index:02d}", name="Supplier source")
                db.add(source)
                await db.flush()
                # Last source exists only in the supplier catalog and must never be crawled.
                if index < count:
                    product = Product(sku=f"CANONICAL-{index:02d}", name="Existing product",
                        source_supplier_product_id=source.id)
                    db.add(product)
                    await db.flush()
                    ids.append(product.id)
                    db.add(ProductSupplySource(product_id=product.id, supplier_product_id=source.id))
            return supplier_id, ids

    def sloger_fact(self, sku, **changes):
        return SlogerObservation(**{"sku": sku, "available": True, "quantity": "8", "quantity_kind": "exact",
            "quantity_relation": "equal", "availability": "Skladom 8 ks", "observed_at": service.now(),
            "purchase_price": "5.10", "price_basis": "net", "currency": "EUR",
            "source_url": "https://www.sloger.sk/shop/tools/brush.html", **changes})

    async def sloger_start(self, supplier_id):
        async with self.transaction() as db:
            await service.request_run(db, "sloger", SupplierAvailabilityRunInput(expected_revision=1))
        async with self.transaction() as db:
            return await service.start_run(db, supplier_id)

    async def test_sloger_scope_uses_only_active_hub_links_and_never_auto_runs(self):
        supplier_id, ids = await self.sloger_fixture(3)
        async with self.transaction() as db:
            inactive = await db.scalar(select(ProductSupplySource).where(ProductSupplySource.product_id == ids[1]))
            inactive.orderable = False
            product = await db.get(Product, ids[2])
            product.is_active = False
            row = await db.get(SupplierAvailabilitySettings, supplier_id)
            row.enabled = True  # Even legacy/out-of-band enabled state cannot start a timer crawl.
        async with self.transaction() as db:
            self.assertEqual(await b2b.scope_skus(db, supplier_id), ["SLG-00"])
            self.assertIsNone(await service.start_run(db, supplier_id))
        plan = await self.sloger_start(supplier_id)
        self.assertEqual(plan["skus"], ["SLG-00"])

    async def test_sloger_batches_resume_successes_and_preserve_failed_observation_expiry(self):
        supplier_id, ids = await self.sloger_fixture()
        old = self.sloger_fact("SLG-01", observed_at=service.now() - timedelta(hours=1), quantity="2")
        async with self.transaction() as db:
            await service.record_sloger_import(db, [old], "draft-before-refresh")
        plan = await self.sloger_start(supplier_id)
        self.assertEqual(len(plan["skus"]), 10)
        results = [SimpleNamespace(sku=sku, observation=None if sku == "SLG-01" else self.sloger_fact(sku),
                                  error="sloger_product_not_found" if sku == "SLG-01" else None)
                   for sku in plan["skus"]]
        async with self.transaction() as db:
            await b2b.accept_batch(db, plan, SimpleNamespace(items=results, aborted=False))
        with self.assertRaises(AvailabilityError):
            async with self.transaction() as db:
                await b2b.accept_batch(db, plan, SimpleNamespace(items=results, aborted=False))
        async with self.transaction() as db:
            resumed = await service.resume_run(db, supplier_id, plan["run_id"])
            self.assertEqual((resumed["cursor"], resumed["skus"]), (10, ["SLG-10", "SLG-11"]))
            previous = await db.get(SupplierAvailabilityObservation, (supplier_id, "SLG-01"))
            self.assertEqual(previous.observed_at, old.observed_at)
            self.assertEqual(previous.expires_at, old.observed_at + timedelta(seconds=21600))
            self.assertEqual((await service.project(db, [ids[0]]))[ids[0]]["label"], "do 2-4 dní")
            self.assertIsNone((await db.get(SupplierAvailabilitySettings, supplier_id)).last_success_at)
        async with self.transaction() as db:
            await b2b.accept_batch(db, resumed, SimpleNamespace(aborted=False, items=[
                SimpleNamespace(sku=sku, observation=self.sloger_fact(sku), error=None) for sku in resumed["skus"]]))
        async with self.transaction() as db:
            report = await b2b.latest_progress(db, supplier_id)
            self.assertEqual((report["total"], report["processed"], report["succeeded"], report["failed"]), (12, 12, 11, 1))
            self.assertEqual(report["status"], "completed")
            self.assertIsNone(await service.start_run(db, supplier_id))
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM stock_movements")), 0)
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM stock_balances")), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(SupplierProduct).where(
                SupplierProduct.supplier_id == supplier_id)), 13)

    async def test_sloger_fatal_batch_stops_remaining_scope_until_explicit_manual_run(self):
        supplier_id, _ids = await self.sloger_fixture()
        plan = await self.sloger_start(supplier_id)
        async with self.transaction() as db:
            await b2b.accept_batch(db, plan, SimpleNamespace(aborted=True, items=[
                SimpleNamespace(sku=sku, observation=None, error="sloger_auth_failed") for sku in plan["skus"]]))
        async with self.transaction() as db:
            row = await db.get(SupplierAvailabilitySettings, supplier_id)
            self.assertIsNone(row.running_run_id)
            self.assertEqual(row.last_error, "supplier_availability_b2b_aborted")
            self.assertIsNone(await service.resume_run(db, supplier_id, plan["run_id"]))
            self.assertIsNone(await service.start_run(db, supplier_id))
            report = await b2b.latest_progress(db, supplier_id)
            self.assertEqual((report["total"], report["processed"], report["status"]), (12, 10, "failed"))

    async def test_sloger_delayed_batch_does_not_overwrite_newer_import_observation(self):
        supplier_id, _ids = await self.sloger_fixture(1)
        plan = await self.sloger_start(supplier_id)
        older = self.sloger_fact("SLG-00", observed_at=service.now() - timedelta(minutes=1), quantity="2")
        newer = self.sloger_fact("SLG-00", quantity="8")
        async with self.transaction() as db:
            await service.record_sloger_import(db, [newer], "newer-import")
        async with self.transaction() as db:
            await b2b.accept_batch(db, plan, SimpleNamespace(aborted=False, items=[
                SimpleNamespace(sku="SLG-00", observation=older, error=None)]))
        async with self.transaction() as db:
            stored = await db.get(SupplierAvailabilityObservation, (supplier_id, "SLG-00"))
            self.assertEqual(stored.observed_at, newer.observed_at)
            self.assertEqual(stored.quantity, 8)

    async def test_sloger_import_creates_only_supplier_observations_and_no_crawler(self):
        config_io.save_supplier("sloger", {"name": "Sloger"})
        fact = self.sloger_fact("7700860", available=None, quantity=None, quantity_kind="unknown", quantity_relation="unknown")
        async with self.transaction() as db:
            await service.record_sloger_import(db, [fact], "draft-only")
        async with self.transaction() as db:
            supplier_id = await db.scalar(select(Supplier.id).where(Supplier.code == "sloger"))
            row = await db.get(SupplierAvailabilitySettings, supplier_id)
            self.assertFalse(row.enabled)
            self.assertIsNone(row.manual_requested_at)
            self.assertIsNone(row.last_success_at)
            self.assertIsNone(await service.start_run(db, supplier_id))
            stored = await db.get(SupplierAvailabilityObservation, (supplier_id, fact.sku))
            self.assertIsNone(stored.available)
            self.assertIsNone(stored.quantity)
            self.assertNotIn("purchase_price", stored.raw)
            self.assertIsNone(await b2b.latest_progress(db, supplier_id))
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM stock_movements")), 0)

    async def test_sloger_resume_rejects_changed_settings_and_empty_scope_completes(self):
        supplier_id, _ids = await self.sloger_fixture(0)
        plan = await self.sloger_start(supplier_id)
        self.assertEqual(plan["skus"], [])
        async with self.transaction() as db:
            await b2b.accept_batch(db, plan, SimpleNamespace(items=[], aborted=False))
            self.assertEqual((await b2b.latest_progress(db, supplier_id))["processed"], 0)
        plan = await self.sloger_start(supplier_id)
        async with self.transaction() as db:
            await service.configure(db, "sloger", SupplierAvailabilityInput(expected_revision=1, feed_key="b2b",
                interval_seconds=300, freshness_seconds=300))
        with self.assertRaises(AvailabilityError):
            async with self.transaction() as db:
                await service.resume_run(db, supplier_id, plan["run_id"])
