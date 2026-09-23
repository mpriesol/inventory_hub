"""Configuration boundaries and protected routes, without a live shop."""
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from inventory_hub.database import get_session
from inventory_hub.settings import settings
from inventory_hub.routers import stock_settings as routes
from inventory_hub.services import stock_settings as service
from inventory_hub.stock_settings_types import OperationalValues, ShopSettingsInput, WarehouseSettingsInput


def shop_input(**changes):
    return {"shop_code": "biketrek", "expected_revision": 0, "expected_warehouse_revision": 0,
            "overrides": {}, "mode": "manual", "confirmed": True, **changes}


class StockSettingsInputTests(unittest.TestCase):
    def test_strict_ranges_reject_dangerous_or_ambiguous_intervals(self):
        for field, values in {"poll_interval_seconds": [0, 59, 86401, True, "300", 300.5],
                              "max_pages_per_pass": [0, 101], "run_timeout_seconds": [29, 181],
                              "overlap_minutes": [0, 4, 1441], "processing_batch_size": [0, 101]}.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                    OperationalValues(**{field: value})
        with self.assertRaises(ValidationError):
            OperationalValues(retry_base_seconds=3600, retry_max_seconds=300)

    def test_partial_overrides_do_not_reset_inheritance_and_cannot_set_authority(self):
        value = ShopSettingsInput(**shop_input(overrides={"poll_interval_seconds": 120}))
        self.assertEqual(value.overrides, {"poll_interval_seconds": 120})
        for overrides in ({"unknown": 1}, {"mode": 1}, {"processing_paused": 1}, {"poll_interval_seconds": None}):
            with self.subTest(overrides=overrides), self.assertRaises(ValidationError):
                ShopSettingsInput(**shop_input(overrides=overrides))
        for key in ("target_fingerprint", "automation_starts_at", "issue_starts_at", "authorized_policy_revision"):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                ShopSettingsInput(**shop_input(**{key: "injected"}))

    def test_confirmations_and_revisions_are_strict(self):
        for value in (False, 1, "true", None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ShopSettingsInput(**shop_input(confirmed=value))
        for value in (1, "true", None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ShopSettingsInput(**shop_input(fulfillment_confirmed=value))
        for value in (-1, True, "1", 1.1):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ShopSettingsInput(**shop_input(expected_revision=value))
        with self.assertRaises(ValidationError):
            WarehouseSettingsInput(warehouse_code="central", expected_revision=0, values={}, processing_paused="false", confirmed=True)


class StockSettingsRouteTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        async def session():
            self.calls.append(True)
            yield SimpleNamespace()
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[get_session] = session
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.token = "settings-synthetic-operator-token-" + "x" * 24
        patcher = patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr(self.token))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.headers = {"Authorization": "Bearer " + self.token}
        self.requests = [
            ("GET", "/stock-settings/options?shop_code=biketrek", None, "options"),
            ("GET", "/stock-settings/warehouse?warehouse_code=central", None, "warehouse_settings"),
            ("POST", "/stock-settings/shop", shop_input(), "configure_shop"),
            ("POST", "/stock-settings/warehouse", {"warehouse_code": "central", "expected_revision": 0,
                "values": {}, "processing_paused": False, "confirmed": True}, "configure_warehouse")]

    def test_authentication_precedes_every_configuration_operation(self):
        with ExitStack() as stack:
            operations = [stack.enter_context(patch.object(service, name, AsyncMock())) for *_, name in self.requests]
            for method, path, payload, _ in self.requests:
                response = self.client.request(method, path, json=payload)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(self.calls, [])
            for operation in operations:
                operation.assert_not_awaited()

    def test_routes_delegate_once_and_sanitize_errors(self):
        for method, path, payload, name in self.requests:
            with self.subTest(path=path), patch.object(service, name, AsyncMock(return_value={"ok": True})) as operation:
                response = self.client.request(method, path, json=payload, headers=self.headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
                operation.assert_awaited_once()
        with patch.object(service, "configure_shop", AsyncMock(side_effect=service.SettingsError("stock_settings_changed"))):
            response = self.client.post("/stock-settings/shop", json=shop_input(), headers=self.headers)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"]["code"], "stock_settings_changed")
        response = self.client.post("/stock-settings/shop", json=shop_input(overrides={"bad_secret": "sensitive-value"}), headers=self.headers)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("sensitive-value", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")


class ProcessingRouteTests(unittest.TestCase):
    def test_queue_controls_require_token_and_literal_confirmation(self):
        from inventory_hub.routers import order_processing as processing_routes
        from inventory_hub.services import order_processing as processing
        db_calls = []
        async def session():
            db_calls.append(True)
            yield SimpleNamespace()
        app = FastAPI()
        app.include_router(processing_routes.router)
        app.dependency_overrides[get_session] = session
        token = "processing-test-operator-" + "x" * 32
        with TestClient(app) as client, patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr(token)), \
             patch.object(processing, "jobs", AsyncMock(return_value={"jobs": [], "total": 0})) as jobs, \
             patch.object(processing, "refresh", AsyncMock(return_value={"scheduled": 0})) as refresh:
            self.assertEqual(client.get("/order-processing/jobs?shop_code=biketrek").status_code, 401)
            self.assertEqual(client.post("/order-processing/refresh", json={"shop_code": "biketrek", "confirmed": True}).status_code, 401)
            self.assertEqual(db_calls, [])
            jobs.assert_not_awaited()
            refresh.assert_not_awaited()
            headers = {"Authorization": "Bearer " + token}
            for confirmed in (False, 1, "true", None):
                response = client.post("/order-processing/refresh", headers=headers, json={"shop_code": "biketrek", "confirmed": confirmed})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.headers["cache-control"], "no-store")
            response = client.get("/order-processing/jobs?shop_code=biketrek&limit=20&offset=40", headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(jobs.await_args.args[1:], ("biketrek", 20, 40))
            response = client.post("/order-processing/refresh", headers=headers, json={"shop_code": "biketrek", "confirmed": True})
            self.assertEqual(response.status_code, 202)
            refresh.assert_awaited_once()
            refresh.side_effect = service.SettingsError("stock_settings_changed")
            self.assertEqual(client.post("/order-processing/refresh", headers=headers, json={"shop_code": "biketrek", "confirmed": True}).status_code, 409)
