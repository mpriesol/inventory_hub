"""Local supplier repair must remain protected, bounded and free of remote actions."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from inventory_hub.database import get_session
from inventory_hub.routers.supplier_availability import router
from inventory_hub.services import supplier_links
from inventory_hub.services import zookee_code_reconciliation as code_service
from inventory_hub.services.product_editor import EditorError
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
        repair.assert_awaited_once_with(self.db, "paul-lange", after_product_id=40, limit=2, match_catalog=True)

    def test_identity_domain_failure_remains_explicit(self):
        with patch.object(supplier_links, "reconcile_supplier_links",
                          AsyncMock(side_effect=AvailabilityError("supplier_source_multiple_products"))):
            response = self.client.post(self.url, json={}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "supplier_source_multiple_products")

    def test_fresh_historical_code_migration_returns_gone(self):
        with patch.object(code_service, "reconcile_one",
                          AsyncMock(side_effect=EditorError("code_rename_retired", 410))) as reconcile:
            request_id = str(uuid4())
            response = self.client.post("/supplier-availability/zookee/legacy-codes/42",
                                        json={"request_id": request_id}, headers=self.headers)
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()["detail"]["code"], "code_rename_retired")
        self.assertEqual(response.headers["cache-control"], "no-store")
        reconcile.assert_awaited_once_with(self.db, "zookee", 42, UUID(request_id))

    def test_historical_code_result_requires_authentication_and_recovers_by_uuid(self):
        request_id = str(uuid4())
        url = f"/supplier-availability/zookee/legacy-code-results/{request_id}"
        result = {"product_id": 42, "request_id": request_id, "status": "completed"}
        with patch.object(code_service, "read_result", AsyncMock(return_value=result)) as recover:
            unauthorized = self.client.get(url)
            self.assertEqual(unauthorized.status_code, 401)
            self.assertEqual(self.db_calls, [])
            recover.assert_not_awaited()
            response = self.client.get(url, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), result)
        self.assertEqual(response.headers["cache-control"], "no-store")
        recover.assert_awaited_once_with(self.db, "zookee", UUID(request_id))
