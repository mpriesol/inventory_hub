"""Deterministic shared Sloger data: one login, privacy, bounds and serialization."""
import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import SecretStr

from inventory_hub.services import sloger_b2b as b2b, sloger_credentials, sloger_data as service
from inventory_hub.services.catalog import CatalogError
from inventory_hub.settings import settings
from inventory_hub.sloger_probe_types import SlogerBatchResult, SlogerCredentials, SlogerLookupResult, SlogerObservation
from test_sloger_b2b import AUTH, LOGIN, PRODUCT_URL, page, product, response, search


def observation(sku="TEST-42"):
    return b2b._parse_product(b2b._document(product(sku)), sku, PRODUCT_URL)


class SlogerBatchTests(unittest.TestCase):
    def client(self, responses):
        client = MagicMock()
        client.__enter__.return_value = client
        client.request.side_effect = responses
        return client

    def test_batch_uses_one_login_and_one_deadline_for_sequential_exact_skus(self):
        other_url = b2b.ORIGIN + "/shop/tools/test-pump.html"
        client = self.client([response(LOGIN), response(page(AUTH)),
                              response(search(PRODUCT_URL)), response(product()),
                              response(search(other_url)), response(product("TEST-43"))])
        with patch.object(b2b.requests, "Session", return_value=client) as sessions, \
                patch.object(b2b.time, "sleep") as sleep, \
                patch.object(b2b, "_Session", wraps=b2b._Session) as bounded:
            result = b2b._lookup_many("test-user", "test-private-password", ["TEST-42", "TEST-43"])
        self.assertFalse(result.aborted)
        self.assertEqual([item.sku for item in result.items], ["TEST-42", "TEST-43"])
        self.assertTrue(all(item.observation is not None and item.error is None for item in result.items))
        self.assertEqual(result.items[0].observation.quantity, "7")
        self.assertEqual(result.items[0].observation.availability_tiers[1].quantity_relation, "greater_than")
        self.assertEqual(result.items[0].observation.purchase_price, "12.5")
        sessions.assert_called_once_with()
        bounded.assert_called_once_with(client)
        self.assertEqual(sum(call.args[0] == "POST" for call in client.request.call_args_list), 1)
        self.assertEqual(sleep.call_count, 5)
        self.assertEqual(client.request.call_count, 6)
        self.assertNotIn("test-private-password", result.model_dump_json())
        self.assertNotIn("test-user", result.model_dump_json())

    def test_missing_exact_sku_is_nonfatal_but_http_failure_aborts_remaining(self):
        client = self.client([response(LOGIN), response(page(AUTH)), response(search()),
                              response(search(PRODUCT_URL)), response(product("TEST-43"))])
        with patch.object(b2b.requests, "Session", return_value=client), patch.object(b2b.time, "sleep"):
            result = b2b._lookup_many("test-user", "test-private-password", ["TEST-42", "TEST-43"])
        self.assertFalse(result.aborted)
        self.assertEqual(result.items[0].error, "sloger_product_not_found")
        self.assertIsNotNone(result.items[1].observation)

        client = self.client([response(LOGIN), response(page(AUTH)),
                              response(search(PRODUCT_URL)), response(product()), response(status=429)])
        with patch.object(b2b.requests, "Session", return_value=client), patch.object(b2b.time, "sleep"):
            result = b2b._lookup_many("test-user", "test-private-password", ["TEST-42", "TEST-43", "TEST-44"])
        self.assertTrue(result.aborted)
        self.assertIsNotNone(result.items[0].observation)
        self.assertEqual(result.items[1].error, "sloger_b2b_unavailable")
        self.assertEqual(result.items[1].diagnostic.http_status, 429)
        self.assertEqual(result.items[2].error, "sloger_batch_aborted")
        self.assertEqual(client.request.call_count, 5)

    def test_failed_login_never_searches_or_retries_and_reports_every_requested_sku(self):
        client = self.client([response(LOGIN), response(page("Login rejected"))])
        with patch.object(b2b.requests, "Session", return_value=client), patch.object(b2b.time, "sleep"):
            result = b2b._lookup_many("test-user", "test-private-password", ["TEST-42", "TEST-43"])
        self.assertTrue(result.aborted)
        self.assertEqual([item.error for item in result.items], ["sloger_auth_failed", "sloger_batch_aborted"])
        self.assertEqual(client.request.call_count, 2)

    def test_bounds_and_private_input_are_rejected_before_opening_a_session(self):
        invalid = ([], [f"SKU-{i}" for i in range(11)], ["TEST-42", "TEST-42"], [" "], [" SKU"],
                   ["control\n"], ["x" * 101], [None], "TEST-42", ["prefix-test-user"], ["test-private-password"])
        with patch.object(b2b.requests, "Session") as sessions:
            for skus in invalid:
                with self.subTest(case="invalid bounded input"), self.assertRaises(CatalogError) as caught:
                    b2b._lookup_many("test-user", "test-private-password", skus)
                self.assertEqual(caught.exception.code, "sloger_b2b_request_invalid")
                self.assertNotIn("test-private-password", str(caught.exception))
            sessions.assert_not_called()

    def test_special_character_secret_or_wrong_identity_aborts_before_next_product(self):
        for secret in ('quote"password', 'backslash\\password', 'newline\npassword'):
            raw = observation()
            raw["availability_tiers"][0]["label"] = "Private " + secret
            with self.subTest(secret="redacted"), patch.object(b2b, "_login"), \
                    patch.object(b2b, "_lookup_in_session", return_value=raw) as lookup:
                result = b2b._lookup_many("test-user", secret, ["TEST-42", "TEST-43"])
            self.assertTrue(result.aborted)
            self.assertEqual([item.error for item in result.items], ["sloger_b2b_invalid_evidence", "sloger_batch_aborted"])
            lookup.assert_called_once()
            self.assertNotIn(secret, result.model_dump_json())
            self.assertNotIn(json.dumps(secret)[1:-1], result.model_dump_json())
        with patch.object(b2b, "_login"), patch.object(b2b, "_lookup_in_session", return_value=observation("WRONG")):
            result = b2b._lookup_many("test-user", "test-private-password", ["TEST-42"])
        self.assertTrue(result.aborted)
        self.assertEqual(result.items[0].error, "sloger_b2b_invalid_evidence")

    def test_deadline_aborts_whole_batch_without_new_login(self):
        client = self.client([])
        with patch.object(b2b.requests, "Session", return_value=client), \
                patch.object(b2b, "_login") as login, \
                patch.object(b2b.time, "monotonic", side_effect=[0, b2b.MAX_SECONDS + 1]):
            result = b2b._lookup_many("test-user", "test-private-password", ["TEST-42", "TEST-43"])
        self.assertTrue(result.aborted)
        self.assertEqual(result.items[0].diagnostic.reason, "deadline")
        self.assertEqual(result.items[1].error, "sloger_batch_aborted")
        login.assert_called_once()
        client.request.assert_not_called()


class SharedDataServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.credentials = SlogerCredentials(username="stored-user", password="stored-private-password")
        self.result = SlogerBatchResult(items=[SlogerLookupResult(
            sku="TEST-42", observation=SlogerObservation.model_validate(observation()))])
        self.locked = False
        self.connections = []

        @asynccontextmanager
        async def connect():
            connection = SimpleNamespace(commit=AsyncMock(), invalidate=AsyncMock())

            async def acquire(*args):
                if self.locked:
                    return False
                self.locked = True
                return True

            async def release(*args):
                self.locked = False

            connection.scalar = AsyncMock(side_effect=acquire)
            connection.execute = AsyncMock(side_effect=release)
            self.connections.append(connection)
            yield connection

        self.adapter = AsyncMock(return_value=self.result)
        self.loader = MagicMock(return_value=self.credentials)
        for item in (patch.object(service.database, "_engine", SimpleNamespace(connect=connect)),
                     patch.object(b2b, "lookup_products", self.adapter),
                     patch.object(sloger_credentials, "load", self.loader)):
            item.start()
            self.addCleanup(item.stop)

    async def test_saved_login_used_once_with_ai_disabled_and_no_budget_or_api_key(self):
        with patch.object(settings, "AI_CONTENT_ENABLED", False), patch.object(settings, "OPENAI_API_KEY", SecretStr("")):
            result = await service.lookup_products(["TEST-42"])
        self.assertEqual(result, self.result)
        self.loader.assert_called_once_with()
        self.adapter.assert_awaited_once_with("stored-user", "stored-private-password", ["TEST-42"])
        self.assertFalse(self.locked)
        self.assertEqual(self.connections[0].commit.await_count, 2)
        self.connections[0].execute.assert_awaited_once()

    async def test_private_input_never_opens_database_lock_or_supplier_network(self):
        for sku in (self.credentials.username, self.credentials.password.get_secret_value()):
            with self.assertRaises(CatalogError) as caught:
                await service.lookup_products(["prefix-" + sku])
            self.assertEqual(caught.exception.code, "sloger_b2b_request_invalid")
        self.assertEqual(self.connections, [])
        self.adapter.assert_not_awaited()

    async def test_busy_is_distinguishable_and_cancelled_caller_keeps_lock_until_source_finishes(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def adapter(*args):
            entered.set()
            await release.wait()
            return self.result

        self.adapter.side_effect = adapter
        first = asyncio.create_task(service.lookup_products(["TEST-42"]))
        await entered.wait()
        first.cancel()
        await asyncio.sleep(0)
        self.assertTrue(self.locked)
        self.assertFalse(first.done())
        with self.assertRaises(CatalogError) as caught:
            await service.lookup_products(["TEST-42"])
        self.assertEqual(caught.exception.code, "sloger_b2b_busy")
        self.assertEqual(self.adapter.await_count, 1)
        self.assertTrue(self.locked)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertFalse(self.locked)
        self.connections[0].execute.assert_awaited_once()
        self.connections[1].execute.assert_not_awaited()

    async def test_unexpected_private_failure_is_redacted_and_releases_lock(self):
        self.adapter.side_effect = RuntimeError("stored-private-password")
        with self.assertRaises(CatalogError) as caught:
            await service.lookup_products(["TEST-42"])
        self.assertEqual(caught.exception.code, "sloger_b2b_unavailable")
        self.assertNotIn("stored-private-password", str(caught.exception))
        self.assertFalse(self.locked)
        self.connections[0].execute.assert_awaited_once()
