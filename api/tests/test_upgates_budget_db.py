"""Independent PostgreSQL sessions enforce one transport request per origin."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg2
from psycopg2.extensions import TRANSACTION_STATUS_IDLE

from inventory_hub.services import upgates_budget as budget

TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")
AT = datetime(2026, 10, 14, 20, 30, tzinfo=timezone.utc)
HEADERS = {"X-Rate-Limit-Hour-Remaining": "2", "X-Rate-Limit-Day-Remaining": "0"}


@unittest.skipUnless(TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class SharedBudgetDatabaseTests(unittest.TestCase):
    def setUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("Budget tests require isolated localhost *_catalog_test")
        self.schema = "budget_test_" + uuid4().hex
        connection = psycopg2.connect(TEST_URL)
        connection.autocommit = True
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'CREATE SCHEMA "{self.schema}"')
                cursor.execute(f'SET search_path TO "{self.schema}"')
                sql = (Path(__file__).resolve().parents[2] / "infra/db-init/024_upgates_api_budget.sql").read_text()
                cursor.execute(sql)
                cursor.execute(sql)  # upgrade is additive and replay-safe
        finally:
            connection.close()
        self.connect_patch = patch.object(budget, "_connect", side_effect=self.connect)
        self.connect_patch.start()
        self.clock_patch = patch.object(budget, "now", return_value=AT)
        self.clock = self.clock_patch.start()
        self.wait_patch = patch.object(budget, "LOCK_WAIT_SECONDS", 0)
        self.wait_patch.start()
        self.key = budget.target_key("https://" + uuid4().hex + ".invalid/api/v2")
        self.permits = []

    def connect(self):
        return psycopg2.connect(TEST_URL, options=f'-c search_path={self.schema} -c statement_timeout=1500')

    def permit(self, key=None):
        permit = budget.DatabasePermit(key or self.key)
        self.permits.append(permit)
        return permit

    def row(self):
        connection = self.connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT hour_remaining, day_remaining, requests_started FROM upgates_api_budget WHERE target_key=%s", (self.key,))
                return cursor.fetchone()
        finally:
            connection.close()

    def tearDown(self):
        for permit in self.permits:
            permit.close()
        self.connect_patch.stop()
        self.clock_patch.stop()
        self.wait_patch.stop()
        connection = psycopg2.connect(TEST_URL)
        connection.autocommit = True
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            connection.close()

    def test_independent_sessions_serialize_same_origin_without_business_transaction(self):
        first = self.permit()
        first.observe(HEADERS, 200)
        self.assertTrue(first.connection.autocommit)
        self.assertEqual(first.connection.get_transaction_status(), TRANSACTION_STATUS_IDLE)
        with self.assertRaises(budget.AdmissionDenied):
            self.permit()
        other = self.permit(budget.target_key("https://" + uuid4().hex + ".invalid"))
        self.assertIsNotNone(other.connection)
        self.assertEqual(self.row(), (2, 0, 1))
        first.close()
        second = self.permit()
        self.assertEqual(self.row(), (1, 0, 2))
        self.assertEqual(second.connection.get_transaction_status(), TRANSACTION_STATUS_IDLE)

    def test_connection_loss_releases_session_lock_and_durable_reservation_survives(self):
        first = self.permit()
        first.observe(HEADERS, 200)
        first.connection.close()  # same server release semantics as worker termination
        second = self.permit()
        self.assertEqual(self.row(), (1, 0, 2))
        second.close()

    def test_429_from_one_session_defers_other_process_sessions_without_more_attempts(self):
        first = self.permit()
        first.observe({**HEADERS, "Retry-After": "120"}, 429)
        first.close()
        self.clock.return_value = AT + timedelta(seconds=10)
        with self.assertRaises(budget.AdmissionDenied) as error:
            self.permit()
        self.assertEqual(error.exception.retry_after, 110)
        self.assertEqual(self.row(), (2, 0, 1))
        self.clock.return_value = AT + timedelta(seconds=121)
        self.permit()
        self.assertEqual(self.row(), (1, 0, 2))

    def test_crash_without_response_consumes_reserved_quota_and_zero_blocks_before_send(self):
        first = self.permit()
        first.observe(HEADERS, 200)
        first.close()
        self.permit().close()  # no headers/response: reservation is not refunded
        self.permit().close()
        with self.assertRaises(budget.AdmissionDenied):
            self.permit()
        self.assertEqual(self.row(), (0, 0, 3))
        self.clock.return_value = AT + timedelta(hours=1)
        self.permit().close()  # one probe only, still no invented quantity
        with self.assertRaises(budget.AdmissionDenied):
            self.permit()
        self.assertEqual(self.row(), (None, 0, 4))


if __name__ == "__main__":
    unittest.main()
