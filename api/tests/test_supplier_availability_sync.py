"""Supplier ambiguity, expiry and queue API without any supplier network access."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from inventory_hub.database import get_session
from inventory_hub.routers.supplier_availability import router
from inventory_hub.services import supplier_availability as service
from inventory_hub.services.supplier_availability_source import AvailabilityError, configuration, observation, parse, quantity
from inventory_hub.settings import settings
from inventory_hub.supplier_availability_types import SupplierAvailabilityInput


class SupplierFactsTests(unittest.TestCase):
    def test_sloger_uses_only_manual_b2b_source_even_with_listing_feeds(self):
        cfg = {"name": "Sloger", "feeds": {"sources": {"products": {"remote": {"url": "https://example.invalid/listing"}}}}}
        with patch("inventory_hub.services.catalog.supplier_config", return_value=cfg):
            self.assertEqual(configuration("sloger", "b2b")[2], "sloger-b2b")
            with self.assertRaises(AvailabilityError):
                configuration("sloger", "products")
        displayed = service.public("sloger", cfg)
        self.assertEqual(displayed["feed_keys"], ["b2b"])
        self.assertTrue(displayed["manual_only"])
        self.assertFalse(displayed["enabled"])
        self.assertEqual(displayed["refresh_scope"], "hub_products")

    def test_unknown_exact_minimum_boolean_and_external_remain_distinct(self):
        cases = [(None, (None, None, "unknown")), ("0", (False, Decimal(0), "exact")),
                 ("6+", (True, Decimal(6), "minimum")), ("0+", (None, Decimal(0), "minimum")),
                 ("áno", (True, None, "boolean")), (False, (False, None, "boolean")),
                 ("1,500", (True, Decimal("1.5"), "exact"))]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(quantity(raw), expected)
        row = observation("SKU", "0", "ano")
        self.assertIs(row.available, True)
        self.assertEqual(row.quantity_kind, "boolean")
        self.assertIsNone(row.quantity)
        self.assertIsNone(observation("SKU", None, "nie").available)

    def test_malformed_values_reject_instead_of_producing_zero(self):
        for value in ("bad", "1.0011", "NaN", "Infinity", "-1", "1++", "1000000000"):
            with self.subTest(value=value), self.assertRaises(AvailabilityError):
                quantity(value)

    def test_expiry_and_shorter_ttl_change_projection_without_a_feed_refresh(self):
        at = datetime(2026, 9, 24, tzinfo=timezone.utc)
        row = SimpleNamespace(available=True, quantity=Decimal(6), quantity_kind="minimum",
            observed_at=at, expires_at=at + timedelta(hours=6))
        fresh = service.project_observation(row, "do 7 dní", "supplier", at)
        self.assertEqual(fresh["label"], "do 7 dní")
        stale = service.project_observation(row, "do 7 dní", "supplier", at + timedelta(hours=6))
        self.assertEqual(stale["label"], "overíme")
        self.assertIsNone(stale["quantity"])
        self.assertTrue(stale["orderable"])
        self.assertFalse(service.project_observation(row, "do 5 dní", "supplier", at + timedelta(hours=1), 3600)["fresh"])
        self.assertFalse(service.project_observation(row, "do 5 dní", "supplier", at + timedelta(hours=7), 86400)["fresh"])

    def test_stock_feed_can_be_minimal_without_product_price_or_name(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "stock.xml"
            path.write_text("<SHOP><SHOPITEM><ITEM_ID>145008</ITEM_ID><STOCK>6+</STOCK></SHOPITEM></SHOP>")
            rows = parse(path, "paul-lange", {"feeds": {"sources": {"stock": {}}}}, "stock", "paul-lange")
            self.assertEqual((rows[0].sku, rows[0].quantity_kind), ("145008", "minimum"))
            path.write_text("<SHOP><SHOPITEM><ITEM_ID>A</ITEM_ID><STOCK>0</STOCK></SHOPITEM>"
                            "<SHOPITEM><ITEM_ID>A</ITEM_ID><STOCK>1</STOCK></SHOPITEM></SHOP>")
            rows = parse(path, "paul-lange", {"feeds": {"sources": {"stock": {}}}}, "stock", "paul-lange")
            self.assertEqual(rows.report()["rejected"], 2)
            self.assertEqual(len(rows), 0)

    def test_empty_and_unknown_only_feeds_cannot_replace_last_success(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "stock.xml"
            path.write_text("<SHOP/>")
            with self.assertRaises(AvailabilityError):
                parse(path, "paul-lange", {"feeds": {"sources": {"stock": {}}}}, "stock", "paul-lange")
            path.write_text("<SHOP><SHOPITEM><ITEM_ID>A</ITEM_ID></SHOPITEM></SHOP>")
            rows = parse(path, "paul-lange", {"feeds": {"sources": {"stock": {}}}}, "stock", "paul-lange")
            self.assertEqual(rows.report()["unknown"], 1)
            self.assertEqual(len(rows), 0)
        self.assertFalse(service.coverage_ok(99, 100, 100))
        self.assertTrue(service.coverage_ok(99, 100, 95))
        self.assertFalse(service.coverage_ok(0, 0, 1))

    def test_settings_are_opt_in_and_validate_strict_intervals(self):
        self.assertFalse(SupplierAvailabilityInput(expected_revision=0).enabled)
        for change in ({"interval_seconds": 299}, {"interval_seconds": True}, {"interval_seconds": "600"},
                       {"freshness_seconds": 300}, {"enabled": "true"}, {"expected_revision": -1},
                       {"min_coverage_percent": 0}, {"min_coverage_percent": 101}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                SupplierAvailabilityInput(**{"expected_revision": 0, **change})

    def test_live_supplier_expiry_is_visible_without_invalidating_editor_metadata(self):
        from inventory_hub.services.product_editor import _make_row
        facts = {"id": 1, "sku": "SKU", "name": "Name", "brand": None, "image_url": None,
                 "attributes": [], "eans": [], "shops": [], "supplier_codes": [],
                 "is_active": True, "group": None,
                 "supplier_availability": {"label": "do 7 dní", "fresh": True}}
        before = _make_row(facts, {}, 0, None, {})
        after = _make_row({**facts, "supplier_availability": {"label": "overíme", "fresh": False}}, {}, 0, None, {})
        self.assertNotEqual(before["supplier_availability"], after["supplier_availability"])
        self.assertEqual(before["snapshot_hash"], after["snapshot_hash"])


class SupplierRoutesTests(unittest.TestCase):
    def test_authentication_no_store_and_sanitized_input(self):
        app = FastAPI()
        app.include_router(router)
        db_calls = []
        async def session():
            db_calls.append(True)
            yield SimpleNamespace()
        app.dependency_overrides[get_session] = session
        token = "synthetic-supplier-token-" + "x" * 32
        with TestClient(app) as client, patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr(token)), \
             patch.object(service, "overview", AsyncMock(return_value={"suppliers": []})) as overview, \
             patch.object(service, "configure", AsyncMock(return_value={"revision": 1})), \
             patch.object(service, "request_run", AsyncMock(return_value={"queued": True})), \
             patch.object(service, "source_observation", AsyncMock(return_value={"status": "not_loaded"})) as observation_read:
            for method, url in (("GET", "/supplier-availability"), ("GET", "/supplier-availability/paul-lange/runs/1/failures"), ("GET", "/supplier-availability/northfinder/observation?sku=103404-281-103"), ("PUT", "/supplier-availability/paul-lange"),
                                ("POST", "/supplier-availability/paul-lange/run")):
                response = client.request(method, url, json={"expected_revision": 0})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(db_calls, [])
            overview.assert_not_awaited()
            observation_read.assert_not_awaited()
            headers = {"Authorization": "Bearer " + token}
            self.assertEqual(client.get("/supplier-availability", headers=headers).json(), {"suppliers": []})
            response = client.get("/supplier-availability/northfinder/observation?sku=103404-281-103", headers=headers)
            self.assertEqual(response.json(), {"status": "not_loaded"})
            self.assertEqual(response.headers["cache-control"], "no-store")
            observation_read.assert_awaited_once()
            self.assertEqual(observation_read.call_args.args[1:], ("northfinder", "103404-281-103"))
            self.assertEqual(client.get("/supplier-availability/northfinder/observation?sku=bad%0Acode", headers=headers).status_code, 422)
            self.assertEqual(client.put("/supplier-availability/paul-lange", headers=headers,
                json={"expected_revision": 0}).status_code, 200)
            self.assertEqual(client.post("/supplier-availability/paul-lange/run", headers=headers,
                json={"expected_revision": 1}).status_code, 202)
            response = client.put("/supplier-availability/paul-lange", headers=headers,
                json={"expected_revision": 0, "unexpected_secret": "DO-NOT-ECHO"})
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("DO-NOT-ECHO", response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")


class SlogerSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_automatic_crawling_is_rejected_before_any_database_mutation(self):
        db = SimpleNamespace(execute=AsyncMock())
        with self.assertRaises(AvailabilityError) as error:
            await service.configure(db, "sloger", SupplierAvailabilityInput(expected_revision=0, feed_key="b2b", enabled=True))
        self.assertEqual(error.exception.code, "supplier_availability_manual_only")
        db.execute.assert_not_awaited()

    async def test_due_ordinary_feed_precedes_resumed_b2b_and_next_cycle_keeps_same_run(self):
        from inventory_hub.services import supplier_availability_worker as worker
        from inventory_hub.services import supplier_availability_b2b as b2b
        from inventory_hub.services import sloger_data
        from inventory_hub import database

        connection = SimpleNamespace(scalar=AsyncMock(return_value=True), commit=AsyncMock(), execute=AsyncMock())
        db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [(42, 99)])),
            scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [1, 42])))

        @asynccontextmanager
        async def connect():
            yield connection

        @asynccontextmanager
        async def transaction():
            yield db

        ongoing = {"supplier": "sloger", "supplier_id": 42, "run_id": 99, "cursor": 10, "skus": ["7700860"]}
        ordinary = {"supplier": "paul-lange", "supplier_id": 1, "run_id": 100}
        with patch.object(database, "_engine", SimpleNamespace(connect=connect)), \
             patch.object(worker, "get_session_context", transaction), \
             patch.object(service, "resume_run", AsyncMock(return_value=ongoing)), \
             patch.object(service, "start_run", AsyncMock(side_effect=[ordinary, None, None])), \
             patch.object(worker, "fetch", Mock(return_value=([], 0))) as fetch, \
             patch.object(service, "accept_run", AsyncMock()) as accept_feed, \
             patch.object(sloger_data, "lookup_products", AsyncMock(return_value="source-result")) as lookup, \
             patch.object(b2b, "accept_batch", AsyncMock()) as accept_b2b:
            self.assertTrue(await worker.cycle())
            lookup.assert_not_awaited()
            accept_feed.assert_awaited_once_with(db, ordinary, [], 0)
            self.assertTrue(await worker.cycle())
            fetch.assert_called_once_with(ordinary)
            lookup.assert_awaited_once_with(["7700860"])
            accept_b2b.assert_awaited_once_with(db, ongoing, "source-result")
