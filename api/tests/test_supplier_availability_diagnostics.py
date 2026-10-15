"""Operator diagnostics preserve the conservative stock publication contract."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import unittest

from inventory_hub.services import supplier_availability as service


class SupplierAvailabilityDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.at = datetime(2026, 10, 15, tzinfo=timezone.utc)
        self.supplier = SimpleNamespace(code="northfinder", name="Northfinder", is_active=True)
        self.product = SimpleNamespace(supplier_sku="103404-281-103", is_active=True, is_discontinued=False, stock_qty=99)
        self.settings = SimpleNamespace(feed_key="stock", running_run_id=None, manual_requested_at=None,
            last_success_at=self.at, last_error=None, freshness_seconds=21600, enabled=False)
        self.observation = SimpleNamespace(observed_at=self.at, expires_at=self.at + timedelta(hours=6),
            available=False, quantity=Decimal(0), quantity_kind="exact")

    def project(self, observation=True, settings=True, feed="stock", error=None):
        return service._source_projection(self.product, self.supplier, self.observation if observation else None,
            self.settings if settings else None, feed, "do 7 dní", error, self.at)

    def test_unknown_and_explicit_zero_have_different_display_state_without_changing_orderability(self):
        zero, absent = self.project(), self.project(observation=False)
        self.assertEqual((zero["availability_state"], zero["quantity"]), ("unavailable", "0"))
        self.assertEqual((absent["availability_state"], absent["quantity"]), ("unknown", None))
        self.assertEqual(zero["label"], "overíme")  # Existing external publication fallback.
        self.assertTrue(zero["orderable"])
        self.assertEqual(absent["status"], "missing_observation")

    def test_setup_evidence_causes_are_distinct_and_keep_raw_identity(self):
        self.assertEqual(self.project(settings=False)["status"], "unconfigured")
        self.settings.last_success_at = None
        self.assertEqual(self.project(observation=False)["status"], "not_loaded")
        self.assertEqual(self.project(feed="products")["status"], "feed_mismatch")
        broken = self.project(error="supplier_availability_source_missing")
        self.assertEqual(broken["status"], "configuration_error")
        self.assertEqual((broken["supplier"], broken["supplier_sku"]), ("northfinder", "103404-281-103"))
        self.assertEqual(broken["action"], "configure")
        self.assertFalse(broken["fresh"])

    def test_expired_evidence_never_falls_back_to_catalog_stock(self):
        self.at += timedelta(hours=7)
        value = self.project()
        self.assertEqual((value["status"], value["availability_state"]), ("stale", "unknown"))
        self.assertIsNone(value["quantity"])
        self.assertEqual(value["action"], "refresh")
        self.assertIsNotNone(value["observed_at"])

    def test_actual_queue_state_is_independent_of_unknown_quantity(self):
        self.settings.manual_requested_at = self.at
        value = self.project(observation=False)
        self.assertEqual((value["refresh_state"], value["action"]), ("queued", "wait"))
        self.settings.running_run_id = 123
        self.assertEqual(self.project(observation=False)["refresh_state"], "running")

    def test_legacy_error_text_is_sanitized(self):
        self.settings.last_error = "https://secret.example/token?private=yes"
        value = self.project()
        self.assertEqual(value["last_error"], "supplier_availability_source_unavailable")
        self.assertNotIn("secret", str(value))

    def test_unconfigured_feed_choice_uses_only_actual_configured_sources(self):
        result = service.public("northfinder", {"feeds": {"sources": {"custom_stock": {"mode": "local", "local_path": "fixture.xml"}}}})
        self.assertEqual((result["feed_key"], result["status"]), ("custom_stock", "unconfigured"))
        self.assertEqual(service.public("northfinder", {})["feed_key"], "")
