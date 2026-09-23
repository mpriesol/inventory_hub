"""FIFO corrections require operator access and never cache sensitive references."""
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from inventory_hub.database import get_session
from inventory_hub.routers import fifo_returns as routes
from inventory_hub.services import fifo_returns as service
from inventory_hub.settings import settings


class FifoReturnRouteTests(unittest.TestCase):
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
        base = {"request_id": "11111111-1111-4111-8111-111111111111", "confirmed": True, "reason": "Verified case"}
        self.requests = [
            ("GET", "/fifo/issues/1/return-options", None, "return_options"),
            ("POST", "/fifo/returns", {**base, "issue_movement_id": 1, "quantity": "1",
                "case_reference": "RMA-1", "condition": "good", "physical_received": True}, "receive_return"),
            ("POST", "/fifo/quarantine/release", {**base, "source_layer_id": 1,
                "target_warehouse_id": 1, "quantity": "1", "condition_verified": True}, "release_quarantine"),
            ("GET", "/fifo/costs/1", None, "cost_options"),
            ("POST", "/fifo/costs/revise", {**base, "root_layer_id": 1, "expected_revision": 0,
                "new_unit_cost": "2.50", "cost_status": "known", "document_reference": "INV-1"}, "revise_cost"),
        ]

    def test_all_routes_authenticate_before_database(self):
        with ExitStack() as stack:
            actions = [stack.enter_context(patch.object(service, name, AsyncMock())) for *_, name in self.requests]
            for method, path, payload, _ in self.requests:
                response = self.client.request(method, path, json=payload)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(self.sessions, [])
            for action in actions:
                action.assert_not_awaited()

    def test_typed_actions_and_no_store(self):
        for method, path, payload, name in self.requests:
            with self.subTest(path=path), patch.object(service, name, AsyncMock(return_value={"ok": True})) as action:
                response = self.client.request(method, path, json=payload, headers=self.headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
                action.assert_awaited_once()

    def test_errors_are_sanitized_and_holds_are_visible(self):
        with patch.object(service, "receive_return", AsyncMock(side_effect=service.FifoReturnError("fifo_return_quantity_exceeds_issue"))):
            _, path, payload, _ = self.requests[1]
            response = self.client.post(path, json=payload, headers=self.headers)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"]["code"], "fifo_return_quantity_exceeds_issue")
        for update in ({"physical_received": "true"}, {"quantity": 1}, {"case_reference": ""},
                       {"private_input": "sensitive-original-reference"}):
            with patch.object(service, "receive_return", AsyncMock()) as action:
                response = self.client.post(path, json={**payload, **update}, headers=self.headers)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertNotIn("sensitive-original-reference", response.text)
                action.assert_not_awaited()
