"""Controlled order-stock input/source contracts; synthetic transport only."""
import json
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from inventory_hub.database import get_session
from inventory_hub.order_stock_types import (
    OrderStockApplyRequest, OrderStockConfigureRequest, OrderStockPreviewRequest,
)
from inventory_hub.routers import order_stock as routes
from inventory_hub.services import order_stock as service
from inventory_hub.services import order_stock_source as source
from inventory_hub.settings import settings


NOW = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
STATUSES = {"order_statuses": [
    {"id": 1, "type": "Received", "descriptions": [{"language_id": "sk", "name": "Prijatá"}]},
    {"id": 2, "type": "Canceled", "descriptions": [{"language_id": "sk", "name": "Storno"}]},
    {"id": 8, "type": "Sent", "descriptions": [{"language_id": "sk", "name": "Odoslaná"}]},
    {"id": 19, "type": "Custom", "descriptions": [{"language_id": "sk", "name": "Vyzdvihnutá"}]},
    {"id": 25, "type": "Custom", "descriptions": [{"language_id": "sk", "name": "Reklamácia"}]},
]}
ACTIONS = {"1": "reserve", "2": "cancel", "8": "issue", "19": "issue", "25": "review"}


def configure_body(**changes):
    return {"shop_code": "biketrek", "warehouse_code": "order-test", "status_hash": "a" * 64,
            "status_actions": dict(ACTIONS), "confirmed": True, **changes}


def preview_body(**changes):
    return {"request_id": str(uuid4()), "shop_code": "biketrek", "order_number": "TEST-01", **changes}


def raw_line(**changes):
    return {"uuid": str(uuid4()), "code": "SKU-A", "title": "Product A", "ean": "", "quantity": "1",
            "unit": "ks", "type": "product", **changes}


def raw_order(*lines, **changes):
    return {"uuid": str(uuid4()), "order_number": "TEST-01", "origin": "eshop",
            "creation_time": NOW.isoformat(), "last_update_time": NOW.isoformat(), "status_id": 1,
            "paid_date": None, "resolved_yn": False, "products": list(lines) or [raw_line()], **changes}


def raw_page(*orders):
    return {"current_page": 1, "number_of_pages": 1, "number_of_items": len(orders), "orders": list(orders)}


class OrderStockInputTests(unittest.TestCase):
    def test_preview_takes_only_source_identity_never_client_stock_facts(self):
        valid = preview_body()
        OrderStockPreviewRequest(**valid)
        for forged in ({"quantity": 99}, {"product_id": 12}, {"action": "issue"}, {"unit_cost": "0"},
                       {"source": raw_order()}, {"warehouse_code": "OTHER"}, {"order_revision": 0}):
            with self.subTest(forged=forged), self.assertRaises(ValidationError):
                OrderStockPreviewRequest(**{**valid, **forged})

    def test_configure_requires_real_confirmation_and_cannot_backdate_the_cutover(self):
        valid = configure_body()
        OrderStockConfigureRequest(**valid)
        for value in (False, None, "true", 1):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                OrderStockConfigureRequest(**{**valid, "confirmed": value})
        for forged in ({"starts_at": "2000-01-01T00:00:00Z"}, {"revision": 1}, {"enabled": True}):
            with self.subTest(forged=forged), self.assertRaises(ValidationError):
                OrderStockConfigureRequest(**{**valid, **forged})

    def test_apply_requires_frozen_hash_and_json_booleans_without_replacement_facts(self):
        valid = {"preview_hash": "a" * 64, "confirmed": True, "physical_confirmed": True}
        OrderStockApplyRequest(**valid)
        OrderStockApplyRequest(**{**valid, "physical_confirmed": False})
        for field in ("confirmed", "physical_confirmed"):
            for value in (None, "true", 1):
                with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                    OrderStockApplyRequest(**{**valid, field: value})
        for patch_data in ({"confirmed": False}, {"preview_hash": "bad"}, {"plan": {}}, {"quantity": "99"},
                           {"lines": []}, {"unit_cost": "1"}, {"source_hash": "a" * 64}):
            with self.subTest(patch_data=patch_data), self.assertRaises(ValidationError):
                OrderStockApplyRequest(**{**valid, **patch_data})


class OrderStockProtectedRouteTests(unittest.TestCase):
    def setUp(self):
        self.db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
        self.dependency_calls = []

        async def session():
            self.dependency_calls.append(True)
            yield self.db

        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[get_session] = session
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.token = "test-only-order-stock-token-" + "x" * 24
        token_patch = patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr(self.token))
        token_patch.start()
        self.addCleanup(token_patch.stop)
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def calls(self):
        identifier = str(uuid4())
        return [
            ("GET", "/order-stock/options?shop_code=biketrek", None, "options"),
            ("POST", "/order-stock/configure", configure_body(), "configure"),
            ("POST", "/order-stock/preview", preview_body(), "preview"),
            ("GET", "/order-stock/previews?shop_code=biketrek", None, "list_previews"),
            ("GET", f"/order-stock/previews/{identifier}", None, "get_preview"),
            ("POST", f"/order-stock/previews/{identifier}/apply",
             {"preview_hash": "a" * 64, "confirmed": True, "physical_confirmed": True}, "apply"),
        ]

    def test_every_new_route_authenticates_before_database_and_source(self):
        with ExitStack() as stack:
            operations = {name: stack.enter_context(patch.object(service, name, AsyncMock(return_value={})))
                          for name in ("options", "configure", "preview", "list_previews", "get_preview", "apply")}
            fetch_source = stack.enter_context(patch.object(source, "load_source", AsyncMock()))
            for method, path, body, _ in self.calls():
                for headers in ({}, {"Authorization": "Bearer wrong-test-token"}):
                    response = self.client.request(method, path, json=body, headers=headers)
                    self.assertEqual(response.status_code, 401, (path, response.text))
                    self.assertEqual(response.headers.get("cache-control"), "no-store")
            self.assertEqual(self.dependency_calls, [])
            for operation in operations.values():
                operation.assert_not_awaited()
            fetch_source.assert_not_awaited()

    def test_authenticated_routes_keep_preview_and_apply_as_explicit_separate_operations(self):
        with ExitStack() as stack:
            operations = {name: stack.enter_context(patch.object(service, name, AsyncMock(return_value={"ok": name})))
                          for name in ("options", "configure", "preview", "list_previews", "get_preview", "apply")}
            for method, path, body, name in self.calls():
                response = self.client.request(method, path, json=body, headers=self.headers)
                self.assertEqual(response.status_code, 200, (path, response.text))
                self.assertEqual(response.headers.get("cache-control"), "no-store")
                self.assertEqual(response.json(), {"ok": name})
            for operation in operations.values():
                operation.assert_awaited_once()

    def test_forged_facts_false_confirmation_and_malformed_unicode_do_not_reach_business_operations(self):
        with patch.object(service, "apply", AsyncMock()) as apply, patch.object(service, "preview", AsyncMock()) as preview:
            for body in ({"preview_hash": "a" * 64, "confirmed": False, "physical_confirmed": True},
                         {"preview_hash": "a" * 64, "confirmed": True, "physical_confirmed": "true"},
                         {"preview_hash": "a" * 64, "confirmed": True, "physical_confirmed": True, "quantity": "100"}):
                response = self.client.post(f"/order-stock/previews/{uuid4()}/apply", json=body, headers=self.headers)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.headers.get("cache-control"), "no-store")
            response = self.client.post("/order-stock/preview", content=json.dumps(preview_body(order_number="\ud800")),
                                        headers={**self.headers, "Content-Type": "application/json"})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.headers.get("cache-control"), "no-store")
            apply.assert_not_awaited()
            preview.assert_not_awaited()
