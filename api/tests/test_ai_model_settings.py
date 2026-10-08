"""Model settings, pricing and access without provider calls."""
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import FastAPI, HTTPException

from inventory_hub.ai_content_models import AiContentSettings
from inventory_hub.routers import ai_content as routes
from inventory_hub.services import ai_content_provider as provider, ai_content_settings as service
from inventory_hub.services.catalog import CatalogError
from test_ai_content import context


class ModelSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_settings_read_preserves_server_default_without_writes(self):
        db = AsyncMock(); db.get.return_value = None; db.add = Mock()
        with patch.object(service.settings, "AI_CONTENT_MODEL", "gpt-5.6-sol"):
            result = await service.read(db)
            self.assertEqual(await service.effective_model(db), "gpt-5.6-sol")
        self.assertEqual((result["revision"], result["source"]), (0, "server"))
        self.assertEqual({m["id"] for m in result["models"]}, set(provider.RATES))
        db.add.assert_not_called(); db.execute.assert_not_awaited(); db.flush.assert_not_awaited()

    async def test_save_is_versioned_and_rejects_unknown_model_before_writes(self):
        db = AsyncMock(); db.get.return_value = None; db.add = Mock()
        with self.assertRaises(CatalogError) as error:
            await service.save(db, service.ModelSettingsSave(expected_revision=0, model="unverified"))
        self.assertEqual(error.exception.code, "ai_model_unpriced")
        db.execute.assert_not_awaited()
        result = await service.save(db, service.ModelSettingsSave(expected_revision=0, model="gpt-6.1-sol"))
        self.assertEqual((result["model"], result["revision"], result["source"]), ("gpt-6.1-sol", 1, "hub"))
        saved = db.add.call_args.args[0]
        self.assertIsInstance(saved, AiContentSettings)
        db.get.return_value = saved
        self.assertEqual(await service.effective_model(db), "gpt-6.1-sol")
        with self.assertRaises(CatalogError) as error:
            await service.save(db, service.ModelSettingsSave(expected_revision=0, model="gpt-6-luna"))
        self.assertEqual(error.exception.code, "ai_settings_changed")
        self.assertEqual(saved.model, "gpt-6.1-sol")


class ModelSettingsRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = AsyncMock(); self.db.get.return_value = None; self.db.add = Mock()
        self.app = FastAPI()
        self.app.include_router(routes.router, prefix="/api")
        async def session():
            yield self.db
        self.app.dependency_overrides[routes.get_session] = session
        self.app.dependency_overrides[routes.access] = lambda: None
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_status_and_settings_share_effective_model(self):
        self.db.get.return_value = AiContentSettings(id=1, model="gpt-6-luna", revision=2,
                                                    updated_at=datetime.now(timezone.utc))
        status = await self.client.get("/api/ai-content/status")
        settings = await self.client.get("/api/ai-content/settings")
        self.assertEqual(status.json()["model"], settings.json()["model"])
        self.assertEqual(settings.json()["revision"], 2)
        self.db.flush.assert_not_awaited()

    async def test_settings_write_rejects_stale_revision_and_requires_access(self):
        response = await self.client.put("/api/ai-content/settings", json={"expected_revision": 3, "model": "gpt-6-luna"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "ai_settings_changed")
        def deny():
            raise HTTPException(401, "Unlock AI content")
        self.app.dependency_overrides[routes.access] = deny
        self.db.reset_mock()
        for method in ("get", "put"):
            response = await getattr(self.client, method)("/api/ai-content/settings",
                **({"json": {"expected_revision": 0, "model": "gpt-6-luna"}} if method == "put" else {}))
            self.assertEqual(response.status_code, 401)
        self.db.get.assert_not_awaited(); self.db.execute.assert_not_awaited()


class ModelPricingTests(unittest.TestCase):
    def test_all_selectable_models_support_same_bounded_output_contract(self):
        for model in provider.RATES:
            with self.subTest(model=model):
                body = provider.request_body(context(model=model, research="official"))
                self.assertEqual(body["model"], model)
                self.assertTrue(body["text"]["format"]["strict"])
                self.assertEqual(body["reasoning"]["effort"], "low")
                self.assertEqual(body["max_tool_calls"], 6)
                self.assertGreater(provider.estimate(context(model=model)), 0)

    def test_cache_writes_replace_standard_input_and_are_not_double_counted(self):
        usage, cost = provider.usage_cost({"usage": {"input_tokens": 1000,
            "input_tokens_details": {"cached_tokens": 200, "cache_write_tokens": 300},
            "output_tokens": 100}}, "gpt-6.1-sol")
        # 500 standard, 200 cached, 300 written, 100 output.
        self.assertEqual(cost, Decimal("0.002770"))
        self.assertEqual(usage["cache_write_tokens"], 300)
        self.assertFalse(usage["long_context"])

    def test_long_context_tier_applies_to_whole_request_after_threshold(self):
        for count, expected in ((272000, "0.277000"), (272001, "0.551520")):
            usage, cost = provider.usage_cost({"usage": {"input_tokens": count,
                "input_tokens_details": {"cached_tokens": 272000}, "output_tokens": 100}}, "gpt-6-astra")
            self.assertEqual(cost, Decimal(expected))
            self.assertEqual(usage["long_context"], count > 272000)

    def test_estimate_reserves_cache_write_rate_and_long_context(self):
        ctx = context(model="gpt-6-luna")
        ctx["facts"] = [{"description": "a" * 280000}]
        estimate = provider.estimate(ctx)
        _, actual = provider.usage_cost({"usage": {"input_tokens": 280000,
            "input_tokens_details": {"cache_write_tokens": 280000}, "output_tokens": provider.MAX_OUTPUT}}, "gpt-6-luna")
        self.assertGreaterEqual(estimate, actual)

    def test_web_search_fees_exclude_open_and_find_but_keep_total_telemetry(self):
        usage, cost = provider.usage_cost({"output": [
            {"type": "web_search_call", "action": {"type": "search"}},
            {"type": "web_search_call", "action": {"type": "open_page"}},
            {"type": "web_search_call", "action": {"type": "find_in_page"}},
            {"type": "web_search_call"},  # legacy provider response
            {"type": "web_search_call", "action": {"type": "unknown_action"}},
        ]}, "gpt-6.1-sol")
        self.assertEqual(cost, Decimal("0.030000"))
        self.assertEqual(usage["web_calls"], 5)
        self.assertEqual(usage["billable_search_calls"], 3)
