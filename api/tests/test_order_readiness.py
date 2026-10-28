"""Publication must not mistake an order notification for complete discovery."""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from inventory_hub.services.order_readiness import evaluate_collection


NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
TARGET = "a" * 64


class OrderReadinessTests(unittest.TestCase):
    def setUp(self):
        self.collector = SimpleNamespace(enabled=True, target_fingerprint=TARGET, last_error=None,
            revision=3, cursor_at=NOW - timedelta(minutes=40), last_completed_at=NOW - timedelta(minutes=39))
        self.webhook = dict(enabled=True, target_fingerprint=TARGET, registration_status="registered",
            registered_at=NOW - timedelta(days=1), registration_checked_at=NOW - timedelta(minutes=10),
            catchup_interval_seconds=3600, pending_count=0, review_count=0, last_error=None)
        self.delta = SimpleNamespace(status="completed", mode="delta", settings_revision=3,
            started_at=self.collector.cursor_at, completed_at=self.collector.last_completed_at,
            configuration_snapshot={"run_timeout_seconds": 180})

    def blockers(self, *, webhook=True, delta=True, at=NOW):
        return evaluate_collection(self.collector, self.webhook if webhook else None,
            self.delta if delta else None, TARGET, 900, at=at)

    def test_registered_delivery_allows_hourly_catchup_but_polling_keeps_900_seconds(self):
        self.assertEqual(self.blockers(), [])
        self.assertEqual(self.blockers(webhook=False), ["stock_sync_orders_stale"])
        self.webhook["enabled"] = False
        self.assertEqual(self.blockers(), ["stock_sync_orders_stale"])
        self.collector.cursor_at = self.collector.last_completed_at = NOW
        self.assertEqual(self.blockers(), [])

    def test_received_notification_never_extends_completed_delta_deadline(self):
        self.webhook["last_received_at"] = NOW
        self.webhook["last_completed_at"] = NOW
        self.delta.completed_at = self.delta.started_at = NOW - timedelta(seconds=4021)
        self.collector.cursor_at = self.delta.started_at
        self.collector.last_completed_at = NOW  # A later reconciliation is insufficient.
        self.assertEqual(self.blockers(), ["stock_sync_orders_stale"])
        self.delta.completed_at = self.delta.started_at = NOW - timedelta(seconds=4020)
        self.collector.cursor_at = self.delta.started_at
        self.assertEqual(self.blockers(), [])

    def test_pending_and_review_notifications_block_even_if_webhooks_are_disabled(self):
        self.collector.cursor_at = self.collector.last_completed_at = NOW
        for enabled in (True, False):
            for field in ("pending_count", "review_count"):
                with self.subTest(enabled=enabled, field=field):
                    self.webhook.update(enabled=enabled, pending_count=0, review_count=0)
                    self.webhook[field] = 1
                    self.assertEqual(self.blockers(), ["stock_sync_order_notifications_pending"])

    def test_registration_requires_same_target_and_recent_remote_verification(self):
        original = dict(self.webhook)
        for changes in ({"target_fingerprint": "b" * 64}, {"registration_status": "pending"},
                        {"registered_at": None}, {"registration_checked_at": None},
                        {"registration_checked_at": NOW - timedelta(hours=6, seconds=1)},
                        {"registration_checked_at": NOW + timedelta(seconds=1)},
                        {"registered_at": NOW}, {"last_error": "registration_failed"}):
            with self.subTest(changes=changes):
                self.webhook = {**original, **changes}
                self.assertEqual(self.blockers(), ["stock_sync_order_webhook_not_ready"])

    def test_new_registration_requires_completed_delta_after_activation(self):
        self.webhook["registered_at"] = self.webhook["registration_checked_at"] = NOW - timedelta(minutes=20)
        self.assertEqual(self.blockers(), ["stock_sync_orders_stale"])
        self.assertEqual(self.blockers(delta=False), ["stock_sync_orders_stale"])

    def test_reconciliation_wrong_revision_and_incomplete_scan_cannot_certify_catchup(self):
        original = vars(self.delta).copy()
        for changes in ({"mode": "reconcile"}, {"status": "running"}, {"settings_revision": 2},
                        {"completed_at": None}, {"completed_at": NOW + timedelta(seconds=1)},
                        {"started_at": NOW}, {"configuration_snapshot": {}},
                        {"configuration_snapshot": {"run_timeout_seconds": 10000}}):
            with self.subTest(changes=changes):
                self.delta = SimpleNamespace(**{**original, **changes})
                self.assertEqual(self.blockers(), ["stock_sync_orders_stale"])

    def test_webhooks_never_override_collector_errors_disabled_state_or_cursor_gap(self):
        original = vars(self.collector).copy()
        for changes in ({"enabled": False}, {"last_error": "order_collection_page_limit"},
                        {"target_fingerprint": "b" * 64}, {"last_completed_at": None},
                        {"cursor_at": None}, {"cursor_at": NOW - timedelta(hours=1)},
                        {"cursor_at": NOW + timedelta(seconds=1)}):
            with self.subTest(changes=changes):
                self.collector = SimpleNamespace(**{**original, **changes})
                self.assertEqual(self.blockers(), ["stock_sync_orders_stale"])

    def test_invalid_catchup_interval_fails_closed(self):
        for value in (None, True, 0, 299, 86401, "3600"):
            with self.subTest(value=value):
                self.webhook["catchup_interval_seconds"] = value
                self.assertEqual(self.blockers(), ["stock_sync_orders_stale"])
