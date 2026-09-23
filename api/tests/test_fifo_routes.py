"""FIFO receiving/cutover routes require operator access and protect references."""
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from inventory_hub.database import get_session
from inventory_hub.routers import fifo as routes
from inventory_hub.services import fifo as service
from inventory_hub.settings import settings


class FifoRouteTests(unittest.TestCase):
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
        token = "fifo-test-operator-" + "x" * 32
        patcher = patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr(token))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.headers = {"Authorization": "Bearer " + token}
        identifier = "11111111-1111-4111-8111-111111111111"
        layer = {"quantity": "1", "unit_cost": None, "cost_status": "unknown",
                 "physical_received_at": "2026-09-20T10:00:00Z", "source_reference": "Delivery note"}
        base = {"request_id": identifier, "sku": "SKU", "warehouse_code": "central", "source_reference": "Verified count",
                "operator_name": "Operator"}
        confirmed = {"preview_hash": "a" * 64, "confirmed": True, "quantities_verified": True, "costs_documented": True}
        self.requests = [
            ("GET", "/fifo/options", None, "options"),
            ("GET", "/fifo/stock?product_id=1&warehouse_code=central", None, "stock"),
            ("GET", "/fifo/history?product_id=1&warehouse_code=central", None, "history"),
            ("GET", "/fifo/movements/1/allocations", None, "allocations"),
            ("POST", "/fifo/cutovers/preview", {**base, "counted_at": "2026-09-23T10:00:00Z", "layers": [layer]}, "cutover_preview"),
            ("GET", f"/fifo/cutovers/{identifier}", None, "get_cutover"),
            ("POST", f"/fifo/cutovers/{identifier}/apply", confirmed, "cutover_apply"),
            ("POST", "/fifo/receipts/preview", {**base, **layer}, "receipt_preview"),
            ("GET", f"/fifo/receipts/{identifier}", None, "get_receipt"),
            ("POST", f"/fifo/receipts/{identifier}/apply", confirmed, "receipt_apply"),
        ]

    def test_all_routes_authenticate_before_database_or_operation(self):
        with ExitStack() as stack:
            actions = [stack.enter_context(patch.object(service, name, AsyncMock())) for *_, name in self.requests]
            for method, path, payload, _ in self.requests:
                response = self.client.request(method, path, json=payload)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(self.sessions, [])
            for action in actions:
                action.assert_not_awaited()

    def test_routes_dispatch_exactly_once_and_never_cache(self):
        for method, path, payload, name in self.requests:
            with self.subTest(path=path), patch.object(service, name, AsyncMock(return_value={"ok": True})) as action:
                response = self.client.request(method, path, json=payload, headers=self.headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
                action.assert_awaited_once()

    def test_confirmation_types_and_error_responses_never_echo_documents(self):
        _, path, payload, name = self.requests[-1]
        for change in ({"confirmed": 1}, {"quantities_verified": False}, {"costs_documented": "true"},
                       {"private_document": "sensitive reference"}):
            with patch.object(service, name, AsyncMock()) as action:
                response = self.client.post(path, json={**payload, **change}, headers=self.headers)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["detail"]["code"], "fifo_invalid_request")
                self.assertNotIn("sensitive reference", response.text)
                action.assert_not_awaited()
        with patch.object(service, name, AsyncMock(side_effect=service.FifoError("stock_publication_warehouse_held"))):
            response = self.client.post(path, json=payload, headers=self.headers)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.headers["cache-control"], "no-store")
