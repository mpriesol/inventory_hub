"""The mobile receipt routes share the operator and no-store boundary."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from inventory_hub.database import get_session
from inventory_hub.routers import mixed_receiving as routes
from inventory_hub.services import mixed_receiving as service
from inventory_hub.settings import settings


class MixedReceivingRouteTests(unittest.TestCase):
    def setUp(self):
        self.database_calls = []

        async def database():
            self.database_calls.append(True)
            yield SimpleNamespace()

        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[get_session] = database
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        token = "mixed-receiving-test-" + "x" * 32
        token_patch = patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr(token))
        token_patch.start()
        self.addCleanup(token_patch.stop)
        self.headers = {"Authorization": "Bearer " + token}
        identifier, scan_id = str(uuid4()), str(uuid4())
        base = "/receiving/batches/" + identifier
        self.actions = [
            ("GET", "/receiving/batches", None, "list_batches"),
            ("POST", "/receiving/batches", {"request_id": identifier}, "create"),
            ("GET", base, None, "summary"),
            ("POST", base + "/scan", {"request_id": scan_id, "code": "4006381333931"}, "scan"),
            ("GET", base + "/manual-product?code=4006381333931", None, "manual_product"),
            ("POST", base + "/manual-scan", {"request_id": scan_id, "code": "4006381333931",
                "confirmed_without_invoice": True, "reason": "Missing invoice"}, "manual_scan"),
            ("POST", base + "/archive", {"request_id": str(uuid4()), "reason": "Empty draft"}, "archive"),
            ("POST", base + "/restore", {"request_id": str(uuid4()), "reason": "Restore draft"}, "archive"),
            ("POST", base + "/scans/" + scan_id + "/reassign",
             {"request_id": str(uuid4()), "supplier": "synthetic", "invoice_no": "TEST-1"}, "reassign"),
            ("POST", base + "/scans/" + scan_id + "/cancel", {"request_id": str(uuid4())}, "cancel"),
            ("POST", base + "/preview", {}, "preview"),
            ("POST", base + "/finalize", {"request_id": str(uuid4()), "preview_hash": "a" * 64,
                                           "confirmed": True}, "finalize"),
        ]

    def test_every_route_denies_anonymous_access_before_using_the_database(self):
        for method, path, body, name in self.actions:
            with self.subTest(route=name), patch.object(service, name, AsyncMock()) as operation:
                response = self.client.request(method, path, json=body)
                self.assertEqual(response.status_code, 401, response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
                operation.assert_not_awaited()
        self.assertEqual(self.database_calls, [])

    def test_authorized_routes_dispatch_once_and_keep_results_private(self):
        for method, path, body, name in self.actions:
            with self.subTest(route=name), patch.object(service, name, AsyncMock(return_value={"ok": True})) as operation:
                response = self.client.request(method, path, json=body, headers=self.headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
                operation.assert_awaited_once()
