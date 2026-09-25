"""Local supplier repair must remain protected, bounded and free of remote actions."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from inventory_hub.database import get_session
from inventory_hub.routers.supplier_availability import router
from inventory_hub.services import supplier_links
from inventory_hub.services.supplier_availability_source import AvailabilityError
from inventory_hub.settings import settings


class SupplierLinkRoutesTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(router)
        self.db = SimpleNamespace()
        self.db_calls = []

        async def session():
            self.db_calls.append(True)
            yield self.db

        app.dependency_overrides[get_session] = session
        self.client = self.enterContext(TestClient(app))
        token = "synthetic-link-repair-token-" + "x" * 32
        self.enterContext(patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr(token)))
        self.headers = {"Authorization": "Bearer " + token}
        self.url = "/supplier-availability/paul-lange/links/reconcile"

    def test_unauthorized_repair_never_opens_database_or_changes_identity(self):
        with patch.object(supplier_links, "reconcile_supplier_links", AsyncMock()) as repair:
            response = self.client.post(self.url, json={})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.db_calls, [])
        repair.assert_not_awaited()

    def test_strict_cursor_and_batch_limits_reject_invalid_input(self):
        with patch.object(supplier_links, "reconcile_supplier_links", AsyncMock()) as repair:
            for body in ({"limit": 0}, {"limit": 501}, {"limit": True}, {"limit": "500"},
                         {"after_product_id": -1}, {"after_product_id": True},
                         {"supplier_url": "DO-NOT-ECHO"}):
                with self.subTest(body=body):
                    response = self.client.post(self.url, json=body, headers=self.headers)
                    self.assertEqual(response.status_code, 422)
                    self.assertEqual(response.headers["cache-control"], "no-store")
                    self.assertNotIn("DO-NOT-ECHO", response.text)
        repair.assert_not_awaited()

    def test_bounded_local_result_retains_conflicts_and_continuation(self):
        result = {"supplier": "paul-lange", "scanned": 2, "linked": 1, "existing": 0,
                  "skipped": 0, "conflicts": [{"product_id": 42, "sku": "PL-001",
                  "supplier_sku": "001", "reason": "identity_conflict"}],
                  "next_after_product_id": 42, "skipped_details": []}
        with patch.object(supplier_links, "reconcile_supplier_links", AsyncMock(return_value=result)) as repair:
            response = self.client.post(self.url, json={"after_product_id": 40, "limit": 2}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), result)
        self.assertEqual(response.headers["cache-control"], "no-store")
        repair.assert_awaited_once_with(self.db, "paul-lange", after_product_id=40, limit=2)

    def test_prefix_domain_failure_remains_explicit(self):
        with patch.object(supplier_links, "reconcile_supplier_links",
                          AsyncMock(side_effect=AvailabilityError("supplier_prefix_conflict"))):
            response = self.client.post(self.url, json={}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "supplier_prefix_conflict")
