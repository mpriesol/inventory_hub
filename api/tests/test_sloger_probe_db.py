"""Read-only Sloger lookups against an isolated PostgreSQL schema and fakes."""
import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from pydantic import SecretStr
from sqlalchemy import func, select, text

import test_catalog_db as catalog_db
from inventory_hub.ai_content_models import AiBatch, AiJob
from inventory_hub.services import ai_content, sloger_credentials, sloger_probe as service
from inventory_hub.services.catalog import CatalogError
from inventory_hub.settings import settings
from inventory_hub.sloger_probe_types import ProbeRequest, SlogerCredentials, SlogerObservation


@unittest.skipUnless(catalog_db.TEST_URL, "Dedicated localhost *_catalog_test database required")
class SlogerProbeDatabaseTests(unittest.IsolatedAsyncioTestCase):
    write_feed = catalog_db.CatalogDatabaseTests.write_feed

    async def asyncSetUp(self):
        await catalog_db.CatalogDatabaseTests.asyncSetUp(self)
        async with self.engine.begin() as connection:
            raw = await connection.get_raw_connection()
            await raw.driver_connection.execute(
                (Path(__file__).resolve().parents[2] / "infra/db-init/005_ai_content.sql").read_text()
            )

        @asynccontextmanager
        async def sessions():
            async with self.sessions() as db:
                yield db
                await db.commit()

        self.facts = SlogerObservation.model_validate({
            "sku": "BT-001", "product_name": "Fixture bicycle", "eans": ["8581234567890"],
            "purchase_price": "120.00", "currency": "EUR", "price_basis": "net",
            "availability": "Skladom", "available": True, "quantity": "3", "quantity_kind": "exact",
            "source_url": "https://www.sloger.sk/fixture-bicycle/", "observed_at": "2026-10-12T12:00:00Z",
        }).model_dump(mode="json")
        self.first = {
            "id": "resp_fixture_tool", "status": "completed",
            "usage": {"input_tokens": 1000, "output_tokens": 100},
            "output": [{"type": "function_call", "name": "lookup_sloger_product",
                        "call_id": "call_fixture", "arguments": json.dumps({"sku": self.facts["sku"]})}],
        }
        self.second = {
            "id": "resp_fixture_result", "status": "completed",
            "usage": {"input_tokens": 1500, "output_tokens": 200},
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": json.dumps(self.facts)}]}],
        }
        self.provider = AsyncMock(side_effect=[self.first, self.second])
        self.adapter = AsyncMock(return_value=self.facts)
        self.patches = [
            patch.object(service, "get_session_context", sessions),
            patch.object(service, "call_provider", self.provider),
            patch.object(service, "lookup_product", self.adapter),
            patch.object(settings, "AI_CONTENT_ENABLED", True),
            patch.object(settings, "OPENAI_API_KEY", SecretStr("fixture-no-network")),
            patch.object(settings, "AI_CONTENT_JOB_USD", 1),
            patch.object(settings, "AI_CONTENT_MONTHLY_USD", 20),
        ]
        for patcher in self.patches:
            patcher.start()

    async def asyncTearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        await catalog_db.CatalogDatabaseTests.asyncTearDown(self)

    def request(self, **changes):
        return ProbeRequest.model_validate({
            "request_id": str(uuid4()), "username": "sloger-fixture-user",
            "password": "fixture-password-do-not-persist", "sku": self.facts["sku"], **changes,
        })

    async def job(self, request):
        async with self.sessions() as db:
            return await db.get(AiJob, request.request_id.hex)

    async def audit_counts(self):
        async with self.sessions() as db:
            return (await db.scalar(select(func.count()).select_from(AiBatch)),
                    await db.scalar(select(func.count()).select_from(AiJob)))

    async def other_table_rows(self):
        """Detect any inserts, deletes or updates outside the two permitted audit tables."""
        async with self.sessions() as db:
            tables = (await db.scalars(text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = :schema AND table_type = 'BASE TABLE' ORDER BY table_name"
            ), {"schema": self.schema})).all()
            snapshot = {}
            for table in tables:
                if table in {"ai_content_batches", "ai_content_jobs"}:
                    continue
                # Identifiers come from this test's guarded schema, never request input.
                quoted = '"' + table.replace('"', '""') + '"'
                rows = (await db.scalars(text(f"SELECT to_jsonb(item) FROM {quoted} AS item"))).all()
                snapshot[table] = sorted(json.dumps(row, sort_keys=True) for row in rows)
            return snapshot

    async def test_completed_replay_is_paid_once_and_only_persists_audit(self):
        before = await self.other_table_rows()
        request = self.request()
        result = await service.probe(request)
        self.assertEqual(result["status"], "lookup_completed")
        self.assertEqual(result["result"], self.facts)
        self.assertEqual(await service.probe(request), result)
        self.assertEqual(await service.read(request.request_id.hex), result)
        self.assertEqual(self.provider.await_count, 2)
        self.adapter.assert_awaited_once_with(
            request.username, request.password.get_secret_value(), request.sku
        )
        self.assertEqual(await self.audit_counts(), (1, 1))
        self.assertEqual(await self.other_table_rows(), before)

        job = await self.job(request)
        self.assertEqual(job.kind, "supplier_lookup")
        self.assertEqual(job.reserved_usd, Decimal(0))
        self.assertGreater(job.actual_usd, 0)
        async with self.sessions() as db:
            self.assertEqual(await ai_content.budget(db), job.actual_usd)
            persisted = await db.scalar(text(
                "SELECT to_jsonb(item)::text FROM ai_content_jobs AS item WHERE id = :id"
            ), {"id": job.id})
        provider_requests = json.dumps([call.args[0] for call in self.provider.await_args_list])
        for secret in (request.username, request.password.get_secret_value(), "fixture-no-network"):
            self.assertNotIn(secret, persisted)
            self.assertNotIn(secret, provider_requests)
            self.assertNotIn(secret, json.dumps(result))

    async def test_saved_login_reuse_and_secret_sku_rejection_precede_persistent_audit(self):
        credentials = SlogerCredentials(username="saved-fixture-user", password="saved-fixture-secret")
        with patch.object(sloger_credentials, "load", return_value=credentials) as load:
            for secret in (credentials.username, credentials.password.get_secret_value()):
                with self.assertRaises(CatalogError) as caught:
                    await service.probe(self.request(username=None, password=None, sku="SKU-" + secret))
                self.assertEqual(caught.exception.code, "sloger_probe_request_invalid")
            self.assertEqual(await self.audit_counts(), (0, 0))
            self.provider.assert_not_awaited()
            request = self.request(username=None, password=None)
            result = await service.probe(request)
            self.assertEqual(result["status"], "lookup_completed")
            self.adapter.assert_awaited_once_with(credentials.username, credentials.password.get_secret_value(), request.sku)
            async with self.sessions() as db:
                persisted = await db.scalar(text(
                    "SELECT to_jsonb(item)::text FROM ai_content_jobs AS item WHERE id = :id"
                ), {"id": request.request_id.hex})
            for secret in (credentials.username, credentials.password.get_secret_value()):
                self.assertNotIn(secret, persisted)
                self.assertNotIn(secret, str(self.provider.await_args_list))
            load.reset_mock()
            load.side_effect = AssertionError("Replays must work without stored credentials")
            self.assertEqual(await service.probe(request), result)
            load.assert_not_called()
            self.assertEqual(self.provider.await_count, 2)

    async def test_inflight_replay_preserves_partial_cost_without_repeating_paid_work(self):
        request = self.request()
        entered, release = asyncio.Event(), asyncio.Event()
        count = 0

        async def provider_request(body):
            nonlocal count
            count += 1
            if count == 1:
                return self.first
            entered.set()
            await release.wait()
            return self.second

        self.provider.side_effect = provider_request
        task = asyncio.create_task(service.probe(request))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            replay = await service.probe(request)
            self.assertEqual(replay["status"], "lookup_running")
            self.assertIsNone(replay["result"])
            self.assertEqual(self.provider.await_count, 2)
            self.adapter.assert_awaited_once()
            self.assertEqual(await self.audit_counts(), (1, 1))
            job = await self.job(request)
            self.assertGreater(job.actual_usd, 0)
            self.assertGreater(job.reserved_usd, job.actual_usd)
            async with self.sessions() as db:
                self.assertEqual(await ai_content.budget(db), job.reserved_usd)
        finally:
            release.set()
            completed = await asyncio.wait_for(task, timeout=5)
        self.assertEqual(completed["status"], "lookup_completed")
        self.assertEqual(self.provider.await_count, 2)
        self.adapter.assert_awaited_once()

    async def test_request_uuid_cannot_be_reused_for_a_different_sku(self):
        request = self.request()
        first, created = await service.reserve(request)
        self.assertTrue(created)
        with self.assertRaises(CatalogError) as error:
            await service.probe(request.model_copy(update={"sku": "BT-DIFFERENT"}))
        self.assertEqual(error.exception.code, "sloger_probe_request_reused")
        self.assertEqual(await service.read(request.request_id.hex), first)
        self.assertEqual(await self.audit_counts(), (1, 1))
        self.provider.assert_not_awaited()
        self.adapter.assert_not_awaited()

    async def test_concurrent_reservations_cannot_overspend_monthly_budget(self):
        amount = service.estimate()
        with patch.object(settings, "AI_CONTENT_MONTHLY_USD", amount):
            outcomes = await asyncio.gather(
                service.reserve(self.request()), service.reserve(self.request()), return_exceptions=True
            )
        accepted = [result for result in outcomes if isinstance(result, tuple)]
        rejected = [result for result in outcomes if isinstance(result, CatalogError)]
        self.assertEqual(len(accepted), 1)
        self.assertTrue(accepted[0][1])
        self.assertEqual([error.code for error in rejected], ["ai_monthly_budget"])
        self.assertEqual(await self.audit_counts(), (1, 1))
        async with self.sessions() as db:
            self.assertEqual(await ai_content.budget(db), amount)
        self.provider.assert_not_awaited()
        self.adapter.assert_not_awaited()

    async def test_partial_known_cost_and_uncertain_result_keep_full_reservation(self):
        request = self.request()
        amount = service.estimate()
        self.provider.side_effect = [self.first, CatalogError("ai_outcome_unknown", "Synthetic timeout", 502)]
        with patch.object(settings, "AI_CONTENT_MONTHLY_USD", amount):
            result = await service.probe(request)
            job = await self.job(request)
            self.assertEqual(result["status"], "lookup_uncertain")
            self.assertEqual(result["error"], "ai_outcome_unknown")
            self.assertIsNone(result["result"])
            self.assertGreater(job.actual_usd, 0)
            self.assertGreater(amount, job.actual_usd)
            self.assertEqual(job.reserved_usd, amount)
            self.assertEqual(len(job.usage["calls"]), 1)
            async with self.sessions() as db:
                self.assertEqual(await ai_content.budget(db), amount)
            self.assertEqual(await service.probe(request), result)
            with self.assertRaises(CatalogError) as error:
                await service.probe(self.request())
            self.assertEqual(error.exception.code, "ai_monthly_budget")
        self.assertEqual(self.provider.await_count, 2)
        self.adapter.assert_awaited_once()
        self.assertEqual(await self.audit_counts(), (1, 1))

    async def test_definitive_adapter_failure_releases_unused_budget_without_secret_text(self):
        request = self.request()
        self.adapter.side_effect = CatalogError(
            "sloger_auth_failed", request.password.get_secret_value(), 401
        )
        result = await service.probe(request)
        job = await self.job(request)
        self.assertEqual(result["status"], "lookup_failed")
        self.assertEqual(result["error"], "sloger_auth_failed")
        self.assertEqual(job.reserved_usd, 0)
        self.assertGreater(job.actual_usd, 0)
        async with self.sessions() as db:
            self.assertEqual(await ai_content.budget(db), job.actual_usd)
        self.assertEqual(await service.probe(request), result)
        self.provider.assert_awaited_once()
        self.adapter.assert_awaited_once()
        self.assertNotIn(request.password.get_secret_value(), json.dumps(result))
        self.assertNotIn(request.password.get_secret_value(), json.dumps(job.events))


if __name__ == "__main__":
    unittest.main()
