"""Supplier ambiguity, expiry and queue API without any supplier network access."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from inventory_hub.database import get_session
from inventory_hub.routers.supplier_availability import router
from inventory_hub.services import supplier_availability as service
from inventory_hub.services.supplier_availability_source import AvailabilityError, observation, parse, quantity
from inventory_hub.settings import settings
from inventory_hub.supplier_availability_types import SupplierAvailabilityInput


class SupplierFactsTests(unittest.TestCase):
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
            with self.assertRaises(AvailabilityError):
                parse(path, "paul-lange", {"feeds": {"sources": {"stock": {}}}}, "stock", "paul-lange")

    def test_empty_and_unknown_only_feeds_cannot_replace_last_success(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "stock.xml"
            for content in ("<SHOP/>", "<SHOP><SHOPITEM><ITEM_ID>A</ITEM_ID></SHOPITEM></SHOP>"):
                path.write_text(content)
                with self.assertRaises(AvailabilityError):
                    parse(path, "paul-lange", {"feeds": {"sources": {"stock": {}}}}, "stock", "paul-lange")
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
             patch.object(service, "request_run", AsyncMock(return_value={"queued": True})):
            for method, url in (("GET", "/supplier-availability"), ("PUT", "/supplier-availability/paul-lange"),
                                ("POST", "/supplier-availability/paul-lange/run")):
                response = client.request(method, url, json={"expected_revision": 0})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(db_calls, [])
            overview.assert_not_awaited()
            headers = {"Authorization": "Bearer " + token}
            self.assertEqual(client.get("/supplier-availability", headers=headers).json(), {"suppliers": []})
            self.assertEqual(client.put("/supplier-availability/paul-lange", headers=headers,
                json={"expected_revision": 0}).status_code, 200)
            self.assertEqual(client.post("/supplier-availability/paul-lange/run", headers=headers,
                json={"expected_revision": 1}).status_code, 202)
            response = client.put("/supplier-availability/paul-lange", headers=headers,
                json={"expected_revision": 0, "unexpected_secret": "DO-NOT-ECHO"})
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("DO-NOT-ECHO", response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
