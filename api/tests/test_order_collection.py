"""Collection controls and worker orchestration; no database or live transport."""
import asyncio
import json
import unittest
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from inventory_hub.database import get_session
from inventory_hub.order_collection_types import CollectionConfigure, CollectionConfirmation, StockProjectionRequest
from inventory_hub.routers import order_collection as routes
from inventory_hub.services import order_collection as service
from inventory_hub.services import order_collection_worker as worker
from inventory_hub.services import order_stock, stock_projection
from inventory_hub.services.stock_settings import SettingsError
from inventory_hub.stock_settings_types import OperationalValues
from inventory_hub.services.order_collection_source import CollectionSourceError
from inventory_hub.services.upgates import UpgatesClient
from inventory_hub.settings import settings


START = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)


def confirmation(**changes):
    return {"shop_code": "biketrek", "expected_revision": 1, "confirmed": True, **changes}


class CollectionInputTests(unittest.TestCase):
    def test_confirmation_and_enabled_require_literal_json_booleans(self):
        CollectionConfigure(**confirmation(enabled=True))
        CollectionConfigure(**confirmation(enabled=False))
        for value in (False, None, "true", "false", 0, 1):
            with self.subTest(confirmed=value), self.assertRaises(ValidationError):
                CollectionConfirmation(**confirmation(confirmed=value))
        for value in (None, "true", "false", 0, 1):
            with self.subTest(enabled=value), self.assertRaises(ValidationError):
                CollectionConfigure(**confirmation(enabled=value))
        with self.assertRaises(ValidationError):
            CollectionConfirmation(shop_code="biketrek")

    def test_revision_is_optional_for_initial_configuration_otherwise_a_positive_strict_integer(self):
        self.assertIsNone(CollectionConfigure(**confirmation(expected_revision=None, enabled=True)).expected_revision)
        self.assertEqual(CollectionConfirmation(**confirmation(expected_revision=2)).expected_revision, 2)
        for value in (True, False, "1", 1.0, 0, -1):
            with self.subTest(revision=value), self.assertRaises(ValidationError):
                CollectionConfirmation(**confirmation(expected_revision=value))

    def test_inputs_cannot_supply_source_or_stock_facts_or_advance_cursors(self):
        for field, value in (("cursor_at", START.isoformat()), ("starts_at", START.isoformat()),
                             ("target_fingerprint", "a" * 64), ("quantity", 20), ("stock", 20),
                             ("source", {}), ("external_write_enabled", True)):
            for model, body in ((CollectionConfirmation, confirmation()),
                                (CollectionConfigure, confirmation(enabled=True)),
                                (StockProjectionRequest, {"shop_code": "biketrek", "skus": ["SKU-1"]})):
                with self.subTest(model=model.__name__, field=field), self.assertRaises(ValidationError):
                    model(**{**body, field: value})

    def test_skus_are_exact_identifiers_without_normalization_or_implicit_expansion(self):
        values = ["SKU-1", "sku-1", "Diel žltý", "A/B", "A" * 100]
        self.assertEqual(StockProjectionRequest(shop_code="biketrek", skus=values).skus, values)
        hundred = [f"SKU-{number}" for number in range(100)]
        self.assertEqual(StockProjectionRequest(shop_code="xtrek", skus=hundred).skus, hundred)
        invalid = [[], hundred + ["ONE-MORE"], ["DUP", "DUP"], [""], [" SKU"], ["SKU "],
                   ["\u00a0SKU"], ["A" * 101], ["A\x00B"], ["A\nB"], ["A\tB"], ["A\x7fB"],
                   ["\ud800"], ["\udfff"], [1], [True], [None], "SKU-1"]
        for values in invalid:
            with self.subTest(values=repr(values)), self.assertRaises(ValidationError):
                StockProjectionRequest(shop_code="biketrek", skus=values)

    def test_shop_identity_is_strict_and_cannot_be_an_arbitrary_target(self):
        for value in (None, True, 1, "", "BIKETREK", " biketrek", "biketrek\n", "../biketrek", "https://example.test"):
            with self.subTest(shop=value), self.assertRaises(ValidationError):
                CollectionConfirmation(**confirmation(shop_code=value))


class CollectionProtectedRouteTests(unittest.TestCase):
    def setUp(self):
        self.db = SimpleNamespace(execute=AsyncMock(), scalar=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
        self.dependency_calls = []

        async def session():
            self.dependency_calls.append(True)
            yield self.db

        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[get_session] = session
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.token = "collection-test-only-operator-token-" + "x" * 24
        token_patch = patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr(self.token))
        token_patch.start()
        self.addCleanup(token_patch.stop)
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def calls(self):
        return [
            ("GET", "/order-collection/status?shop_code=biketrek", None, "status", 200),
            ("POST", "/order-collection/configure", confirmation(enabled=True), "configure", 200),
            ("POST", "/order-collection/refresh", confirmation(), "refresh", 202),
            ("GET", "/order-collection/inbox?shop_code=biketrek&limit=25&offset=50", None, "inbox", 200),
            ("GET", "/order-collection/runs?shop_code=biketrek", None, "runs", 200),
            ("POST", "/order-collection/stock-preview", {"shop_code": "biketrek", "skus": ["SKU-2", "SKU-1"]}, "preview", 200),
        ]

    def operations(self, stack):
        operations = {name: stack.enter_context(patch.object(service, name, AsyncMock(return_value={"ok": name})))
                      for name in ("status", "configure", "refresh", "inbox", "runs")}
        operations["preview"] = stack.enter_context(patch.object(stock_projection, "preview", AsyncMock(return_value={"ok": "preview"})))
        return operations

    def test_all_six_routes_authenticate_before_database_business_or_network(self):
        with ExitStack() as stack:
            operations = self.operations(stack)
            client_factory = stack.enter_context(patch.object(UpgatesClient, "from_shop"))
            stock_apply = stack.enter_context(patch.object(order_stock, "apply", AsyncMock()))
            for method, path, body, _, _ in self.calls():
                for headers in ({}, {"Authorization": "Bearer invalid"}, {"Authorization": self.token}):
                    with self.subTest(path=path, headers=bool(headers)):
                        response = self.client.request(method, path, json=body, headers=headers)
                        self.assertEqual(response.status_code, 401, response.text)
                        self.assertEqual(response.headers.get("cache-control"), "no-store")
            self.assertEqual(self.dependency_calls, [])
            for operation in operations.values():
                operation.assert_not_awaited()
            client_factory.assert_not_called()
            stock_apply.assert_not_awaited()

    def test_missing_server_token_fails_closed_without_database_access(self):
        with patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr("")):
            for method, path, body, _, _ in self.calls():
                response = self.client.request(method, path, json=body, headers=self.headers)
                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertEqual(self.dependency_calls, [])

    def test_success_responses_are_not_cached_and_delegate_exactly(self):
        with ExitStack() as stack:
            operations = self.operations(stack)
            for method, path, body, name, status in self.calls():
                response = self.client.request(method, path, json=body, headers=self.headers)
                self.assertEqual(response.status_code, status, (path, response.text))
                self.assertEqual(response.headers.get("cache-control"), "no-store")
                self.assertEqual(response.json(), {"ok": name})
            for operation in operations.values():
                operation.assert_awaited_once()
            operations["status"].assert_awaited_once_with(self.db, "biketrek")
            operations["inbox"].assert_awaited_once_with(self.db, "biketrek", 25, 50)
            operations["runs"].assert_awaited_once_with(self.db, "biketrek")
            operations["preview"].assert_awaited_once_with(self.db, "biketrek", ["SKU-2", "SKU-1"])
            self.assertIsInstance(operations["configure"].await_args.args[1], CollectionConfigure)
            self.assertIsInstance(operations["refresh"].await_args.args[1], CollectionConfirmation)

    def test_business_errors_are_safe_no_store_responses_for_every_route(self):
        with ExitStack() as stack:
            operations = self.operations(stack)
            for method, path, body, name, _ in self.calls():
                error_type = stock_projection.StockProjectionError if name == "preview" else service.CollectionError
                status = 429 if name == "refresh" else 409
                operations[name].side_effect = error_type("synthetic_safe_code", status)
                response = self.client.request(method, path, json=body, headers=self.headers)
                self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(response.headers.get("cache-control"), "no-store")
                self.assertEqual(response.json(), {"detail": {"code": "synthetic_safe_code", "message": "synthetic_safe_code"}})

    def test_validation_errors_do_not_echo_submitted_values_or_reach_business_operations(self):
        secret = "PRIVATE-NOT-FOR-RESPONSE"
        invalid = [
            ("POST", "/order-collection/configure", confirmation(enabled="true")),
            ("POST", "/order-collection/configure", confirmation(enabled=True, confirmed=False)),
            ("POST", "/order-collection/refresh", confirmation(expected_revision=True)),
            ("POST", "/order-collection/refresh", confirmation(confirmed="true")),
            ("POST", "/order-collection/refresh", confirmation(source=secret)),
            ("POST", "/order-collection/stock-preview", {"shop_code": "biketrek", "skus": ["\ud800" + secret]}),
            ("POST", "/order-collection/stock-preview", {"shop_code": "biketrek", "skus": [secret], "stock": 999}),
            ("GET", f"/order-collection/status?shop_code={secret}", None),
            ("GET", "/order-collection/inbox?shop_code=biketrek&limit=101", None),
            ("GET", "/order-collection/inbox?shop_code=biketrek&offset=-1", None),
            ("GET", "/order-collection/inbox?shop_code=biketrek&offset=1000001", None),
            ("GET", "/order-collection/runs", None),
        ]
        with ExitStack() as stack:
            operations = self.operations(stack)
            for method, path, body in invalid:
                response = self.client.request(method, path, content=json.dumps(body) if body is not None else None,
                                               headers={**self.headers, "Content-Type": "application/json"})
                self.assertEqual(response.status_code, 422, (path, response.text))
                self.assertEqual(response.headers.get("cache-control"), "no-store")
                self.assertEqual(response.json(), {"detail": {"code": "order_collection_invalid_request",
                                                           "message": "order_collection_invalid_request"}})
                self.assertNotIn(secret, response.text)
            response = self.client.post("/order-collection/stock-preview", content='{"skus":',
                                        headers={**self.headers, "Content-Type": "application/json"})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.headers.get("cache-control"), "no-store")
            for operation in operations.values():
                operation.assert_not_awaited()

    def test_stock_preview_only_delegates_to_read_only_projection_without_collecting_or_applying(self):
        with ExitStack() as stack:
            operations = self.operations(stack)
            collect = stack.enter_context(patch.object(worker, "collect", AsyncMock()))
            apply = stack.enter_context(patch.object(order_stock, "apply", AsyncMock()))
            client_factory = stack.enter_context(patch.object(UpgatesClient, "from_shop"))
            response = self.client.post("/order-collection/stock-preview",
                json={"shop_code": "xtrek", "skus": ["Exact-case", "exact-case"]}, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)
            operations["preview"].assert_awaited_once_with(self.db, "xtrek", ["Exact-case", "exact-case"])
            for name, operation in operations.items():
                if name != "preview":
                    operation.assert_not_awaited()
            collect.assert_not_awaited()
            apply.assert_not_awaited()
            client_factory.assert_not_called()
            self.db.execute.assert_not_awaited()
            self.db.commit.assert_not_awaited()


def entry(number, *, at=START, identifier=None):
    return {"uuid": identifier or str(uuid4()), "order_number": number, "updated_at": at.isoformat()}


def page(*entries, pages=1, total=None, more=False):
    return {"entries": list(entries), "number_of_pages": pages,
            "number_of_items": len(entries) if total is None else total, "has_more": more}


class CollectionWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plan = {"id": str(uuid4()), "shop_code": "biketrek", "created_from": START,
                     "changed_from": START, "created_to": START + timedelta(hours=1),
                     "target_fingerprint": "test-target",
                     "configuration_hash": "a" * 64,
                     "configuration_snapshot": OperationalValues().model_dump()}
        self.db = object()
        self.events = []

        @asynccontextmanager
        async def context():
            self.events.append("begin")
            yield self.db
            self.events.append("commit")

        async def check(db, identifier):
            self.assertIs(db, self.db)
            self.assertEqual(identifier, self.plan["id"])
            self.events.append("check")

        async def save(db, identifier, entries):
            self.assertIs(db, self.db)
            self.assertEqual(identifier, self.plan["id"])
            self.events.append(("save", [row["order_number"] for row in entries]))

        async def complete(db, identifier):
            self.assertIs(db, self.db)
            self.assertEqual(identifier, self.plan["id"])
            self.events.append("complete")

        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(worker, "get_session_context", context))
        self.check = self.stack.enter_context(patch.object(service, "check_run", AsyncMock(side_effect=check)))
        self.save = self.stack.enter_context(patch.object(service, "save_page", AsyncMock(side_effect=save)))
        self.complete = self.stack.enter_context(patch.object(service, "complete_run", AsyncMock(side_effect=complete)))
        self.load = self.stack.enter_context(patch.object(worker, "load_changed_page", AsyncMock()))
        self.stock_apply = self.stack.enter_context(patch.object(order_stock, "apply", AsyncMock()))

    async def test_pages_are_checked_and_saved_separately_before_completion_of_both_scopes(self):
        responses = iter([page(entry("A"), pages=2, total=2, more=True),
                          page(entry("B", at=START + timedelta(minutes=1)), pages=2, total=2),
                          page(entry("DELETED", at=START - timedelta(minutes=1)))])

        async def fetch(*args, **kwargs):
            self.events.append(("fetch", kwargs["deleted"], kwargs["page"]))
            return next(responses)

        self.load.side_effect = fetch
        await worker.collect(self.plan)
        expected = []
        for deleted, number, names in ((False, 1, ["A"]), (False, 2, ["B"]), (True, 1, ["DELETED"])):
            expected.extend(["begin", "check", "commit", ("fetch", deleted, number),
                             "begin", ("save", names), "commit"])
        expected.extend(["begin", "complete", "commit"])
        self.assertEqual(self.events, expected)
        for call in self.load.await_args_list:
            self.assertEqual(call.args, ("biketrek",))
            self.assertEqual(call.kwargs["created_from"], self.plan["created_from"])
            self.assertEqual(call.kwargs["changed_from"], self.plan["changed_from"])
            self.assertEqual(call.kwargs["created_to"], self.plan["created_to"])
            self.assertEqual(call.kwargs["expected_target_fingerprint"], "test-target")
        self.stock_apply.assert_not_awaited()

    async def test_disabled_or_changed_run_stops_before_the_next_network_read(self):
        self.check.side_effect = service.CollectionError("order_collection_interrupted")
        with self.assertRaises(service.CollectionError):
            await worker.collect(self.plan)
        self.load.assert_not_awaited()
        self.save.assert_not_awaited()
        self.complete.assert_not_awaited()

    async def test_source_failure_never_saves_or_completes_the_failed_page(self):
        self.load.side_effect = [page(entry("A")), CollectionSourceError("order_collection_source_unavailable")]
        with self.assertRaises(CollectionSourceError):
            await worker.collect(self.plan)
        self.assertEqual(self.save.await_count, 1)
        self.complete.assert_not_awaited()
        self.stock_apply.assert_not_awaited()

    async def test_cross_page_identity_duplicates_and_reversed_order_stop_without_advancing_completion(self):
        first = entry("A", at=START + timedelta(minutes=1))
        cases = [entry("B", identifier=first["uuid"]), entry("A"), entry("B", at=START)]
        for second in cases:
            with self.subTest(second=second):
                self.save.reset_mock()
                self.load.side_effect = [page(first, pages=2, total=2, more=True), page(second, pages=2, total=2)]
                with self.assertRaises(service.CollectionError) as raised:
                    await worker.collect(self.plan)
                self.assertEqual(raised.exception.code, "order_collection_unstable_scan")
                self.assertEqual(self.save.await_count, 1)
                self.complete.assert_not_awaited()

    async def test_same_order_in_active_and_deleted_scans_is_not_silently_applied_twice(self):
        first = entry("A")
        self.load.side_effect = [page(first), page(dict(first))]
        with self.assertRaises(service.CollectionError) as raised:
            await worker.collect(self.plan)
        self.assertEqual(raised.exception.code, "order_collection_unstable_scan")
        self.assertEqual(self.save.await_count, 1)
        self.complete.assert_not_awaited()

    async def test_scan_metadata_change_and_incomplete_count_never_complete_a_run(self):
        cases = [
            [page(entry("A"), pages=2, total=2, more=True), page(entry("B"), pages=2, total=3)],
            [page(entry("A"), pages=2, total=2, more=True), page(entry("B"), pages=3, total=2)],
            [page(entry("A"), total=2)],
        ]
        for responses in cases:
            with self.subTest(responses=responses):
                self.load.side_effect = responses
                with self.assertRaises(service.CollectionError) as raised:
                    await worker.collect(self.plan)
                self.assertEqual(raised.exception.code, "order_collection_unstable_scan")
                self.complete.assert_not_awaited()

    async def test_scan_is_bounded_both_by_reported_pages_and_actual_iteration(self):
        self.plan["configuration_snapshot"]["max_pages_per_pass"] = 2
        self.load.side_effect = None
        self.load.return_value = page(pages=3)
        with self.assertRaises(service.CollectionError) as raised:
            await worker.collect(self.plan)
        self.assertEqual(raised.exception.code, "order_collection_backlog_limit")
        self.save.assert_not_awaited()
        self.complete.assert_not_awaited()
        self.load.reset_mock()
        self.load.side_effect = [page(entry("A"), pages=2, total=2, more=True),
                                 page(entry("B"), pages=2, total=2, more=True)]
        with self.assertRaises(service.CollectionError) as raised:
            await worker.collect(self.plan)
        self.assertEqual(raised.exception.code, "order_collection_backlog_limit")
        self.assertEqual(self.load.await_count, 2)
        self.complete.assert_not_awaited()

    async def test_configuration_changed_before_next_page_stops_before_another_get(self):
        self.load.return_value = page(entry("A"), pages=2, total=2, more=True)
        self.check.side_effect = [None, service.CollectionError("order_collection_settings_changed")]
        with self.assertRaises(service.CollectionError) as raised:
            await worker.collect(self.plan)
        self.assertEqual(raised.exception.code, "order_collection_settings_changed")
        self.load.assert_awaited_once()
        self.save.assert_awaited_once()
        self.complete.assert_not_awaited()


class CollectionConfigurationErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_policy_or_inactive_warehouse_becomes_safe_collector_error(self):
        for code in ("stock_settings_policy_required", "stock_settings_warehouse_not_found"):
            with self.subTest(code=code), patch.object(service.stock_settings, "effective",
                    AsyncMock(side_effect=SettingsError(code, 404))):
                with self.assertRaises(service.CollectionError) as raised:
                    await service._configuration(object(), 1, lock=True)
                self.assertEqual(raised.exception.code, "order_collection_not_configured")
                self.assertEqual(raised.exception.status, 409)

    async def test_invalid_operational_values_remain_sanitized_specific_error(self):
        with patch.object(service.stock_settings, "effective",
                AsyncMock(side_effect=SettingsError("stock_settings_invalid_values", 422))):
            with self.assertRaises(service.CollectionError) as raised:
                await service._configuration(object(), 1)
            self.assertEqual(raised.exception.code, "stock_settings_invalid_values")
            self.assertEqual(raised.exception.status, 422)


class CollectionWorkerCycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_cycle_uses_frozen_timeout_and_records_settings_error_without_generic_failure(self):
        db = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [])),
                             scalar=AsyncMock(return_value=1))
        connection = SimpleNamespace(scalar=AsyncMock(return_value=True), execute=AsyncMock(), commit=AsyncMock())

        @asynccontextmanager
        async def session():
            yield db

        @asynccontextmanager
        async def connected():
            yield connection

        plan = {"id": str(uuid4()), "configuration_snapshot": {"run_timeout_seconds": 45}}
        with patch.object(worker, "get_session_context", session), \
             patch.object(worker.database, "_engine", SimpleNamespace(connect=Mock(side_effect=connected))), \
             patch.object(service, "start_run", AsyncMock(return_value=plan)), \
             patch.object(worker, "collect", AsyncMock(side_effect=SettingsError("stock_settings_invalid_values", 422))), \
             patch.object(service, "fail_run", AsyncMock()) as fail, \
             patch.object(worker.asyncio, "timeout", wraps=asyncio.timeout) as timeout:
            self.assertTrue(await worker.cycle())
        timeout.assert_called_once_with(45)
        fail.assert_awaited_once_with(db, plan["id"], "stock_settings_invalid_values", None)
        connection.commit.assert_awaited_once()
