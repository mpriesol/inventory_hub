"""Operator authorization and durable action routing without external calls."""
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from inventory_hub.database import get_session
from inventory_hub.settings import settings
from inventory_hub.routers import stock_publication as routes
from inventory_hub.services import stock_publication as service


class PublicationRouteTests(unittest.TestCase):
    def setUp(self):
        self.sessions = []
        async def session():
            self.sessions.append(True)
            yield SimpleNamespace()
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[get_session] = session
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        token = "publication-synthetic-token-" + "x" * 32
        patcher = patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr(token))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.headers = {"Authorization": "Bearer " + token}
        identifier = "11111111-1111-4111-8111-111111111111"
        batch = "/stock-publication/batches/" + identifier
        self.requests = [
            ("GET", "/stock-publication/options?shop_code=biketrek", None, "options"),
            ("POST", "/stock-publication/configure", {"shop_code": "biketrek", "expected_revision": 0,
                "enabled": False, "confirmed": True}, "configure"),
            ("POST", "/stock-publication/holds", {"shop_code": "biketrek", "confirmed": True,
                "external_writers_paused": True, "orders_reconciled": True}, "open_hold"),
            ("POST", "/stock-publication/holds/" + identifier + "/release", {
                "confirmed": True, "maintenance_completed": True}, "release_hold"),
            ("POST", "/stock-publication/preview", {"shop_code": "biketrek", "request_id": identifier,
                "skus": ["SHARED-SKU"]}, "preview"),
            ("GET", "/stock-publication/batches?shop_code=biketrek", None, "batches"),
            ("GET", batch, None, "get_batch"),
            ("POST", batch + "/submit", {"confirmed": True, "preview_hash": "a" * 64}, "submit"),
            ("POST", batch + "/cancel", {"confirmed": True}, "cancel"),
            ("POST", batch + "/verify", {"confirmed": True}, "verify"),
            ("POST", batch + "/resolve", {"confirmed": True, "external_requests_finished": True}, "resolve"),
        ]

    def test_authentication_precedes_database_and_all_actions(self):
        with ExitStack() as stack:
            operations = [stack.enter_context(patch.object(service, name, AsyncMock())) for *_, name in self.requests]
            for method, path, payload, _ in self.requests:
                response = self.client.request(method, path, json=payload)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(self.sessions, [])
            for operation in operations:
                operation.assert_not_awaited()

    def test_exactly_one_call_and_no_store_for_every_action(self):
        for method, path, payload, name in self.requests:
            with self.subTest(path=path), patch.object(service, name, AsyncMock(return_value={"ok": True})) as operation:
                response = self.client.request(method, path, json=payload, headers=self.headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
                operation.assert_awaited_once()

    def test_error_sanitization_and_real_boolean_confirmation(self):
        with patch.object(service, "open_hold", AsyncMock(side_effect=service.PublicationError("stock_publication_busy"))):
            response = self.client.post("/stock-publication/holds", headers=self.headers,
                json={"shop_code": "biketrek", "confirmed": True, "external_writers_paused": True, "orders_reconciled": True})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"]["code"], "stock_publication_busy")
            self.assertEqual(response.headers["cache-control"], "no-store")
        for value in (False, 1, "true", None):
            with patch.object(service, "open_hold", AsyncMock()) as operation:
                response = self.client.post("/stock-publication/holds", headers=self.headers,
                    json={"shop_code": "biketrek", "confirmed": True, "external_writers_paused": value,
                          "orders_reconciled": True})
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("sensitive-value", response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
                operation.assert_not_awaited()
        response = self.client.post("/stock-publication/holds", headers=self.headers,
            json={"shop_code": "biketrek", "confirmed": True, "external_writers_paused": True,
                  "orders_reconciled": True, "unexpected": "sensitive-value"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("sensitive-value", response.text)
