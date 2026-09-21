"""New AI state machine against an isolated PostgreSQL schema and fake providers."""
import json
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from pydantic import SecretStr
from sqlalchemy import select, text

import test_catalog_db as catalog_db
from catalog_fixtures import FakeUpgates
from test_ai_content import content
from inventory_hub import config_io, database
from inventory_hub.ai_content_models import AiContentRevision, AiJob, AiRuleVersion
from inventory_hub.ai_content_types import BatchRequest, ContentReview, JobAction, Policy, RuleBook, Target
from inventory_hub.services import ai_content as service, ai_content_rules as rules, ai_content_worker as worker, catalog, catalog_import
from inventory_hub.services.ai_content_upgates import FIELDS
from inventory_hub.settings import settings


class FakeContentShop(FakeUpgates):
    def get(self, path, params=None):
        data = super().get(path, params)
        if path == "metas":
            data["metas"] += [{**f, "type": "input", "category": "products"} for f in FIELDS]
        return data


@unittest.skipUnless(catalog_db.TEST_URL, "Dedicated localhost *_catalog_test database required")
class AiDatabaseTests(unittest.IsolatedAsyncioTestCase):
    write_feed = catalog_db.CatalogDatabaseTests.write_feed
    refresh = catalog_db.CatalogDatabaseTests.refresh

    async def asyncSetUp(self):
        await catalog_db.CatalogDatabaseTests.asyncSetUp(self)
        async with self.engine.begin() as connection:
            raw = await connection.get_raw_connection()
            await raw.driver_connection.execute((Path(__file__).resolve().parents[2] / "infra/db-init/005_ai_content.sql").read_text())
        await self.refresh()
        async with self.sessions() as db:
            page = await catalog.catalog_page(db, "paul-lange")
            self.id = page.items[0].product.id
        self.clients = {shop: FakeContentShop() for shop in ("biketrek", "xtrek")}
        for shop in self.clients:
            path = config_io.shop_path(shop); path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"upgates_api_base_url": f"https://{shop}.example.test/api/v2", "upgates_login": "fixture", "upgates_api_key": "fixture"}))
        @asynccontextmanager
        async def sessions():
            async with self.sessions() as db:
                yield db
                await db.commit()
        self.patches = [patch.object(database, "_engine", self.engine), patch.object(worker, "get_session_context", sessions),
            patch.object(catalog_import, "get_session_context", sessions),
            patch.object(catalog_import.UpgatesClient, "from_shop", side_effect=lambda shop: self.clients[shop]),
            patch.object(settings, "AI_CONTENT_ENABLED", True), patch.object(settings, "OPENAI_API_KEY", SecretStr("fixture-no-network")),
            patch.object(settings, "AI_CONTENT_MONTHLY_USD", 20)]
        for p in self.patches:
            p.start()
        output = content(evidence=[{"claim": "Popis", "source": f"feed:{self.id}", "quote": "Popis"}])
        self.response = {"id": "resp_fixture", "status": "completed", "usage": {"input_tokens": 1000, "output_tokens": 500},
                         "output": [{"type": "message", "content": [{"type": "output_text", "text": output.model_dump_json()}]}]}
        self.provider = patch.object(worker.provider, "generate", AsyncMock(return_value=self.response))
        self.generate = self.provider.start()

    async def asyncTearDown(self):
        self.provider.stop()
        for p in reversed(self.patches):
            p.stop()
        await catalog_db.CatalogDatabaseTests.asyncTearDown(self)

    def request(self, **changes):
        return BatchRequest.model_validate({"request_id": str(uuid4()), "supplier": "paul-lange", "product_ids": [self.id],
            "ai_product_ids": [self.id], "research": "feed_only", "targets": [{"shop": "biketrek"}], **changes})

    async def create(self, request=None):
        async with self.sessions() as db:
            jobs = await service.create_batch(db, request or self.request())
            await db.commit()
            return jobs

    async def job(self, id):
        async with self.sessions() as db:
            return await service.get_job(db, id)

    async def act(self, id, action):
        async with self.sessions() as db:
            job = await service.get_job(db, id, True)
            await service.action(db, job, JobAction(expected_revision=job.revision, action=action))
            await db.commit()

    async def test_prepare_is_idempotent_and_pins_rules(self):
        request = self.request()
        first = await self.create(request); second = await self.create(request)
        self.assertEqual(first[0]["id"], second[0]["id"])
        self.assertEqual(first[0]["status"], "estimate")
        self.generate.assert_not_called()
        async with self.sessions() as db:
            old = await rules.published(db)
            new = AiRuleVersion(book=old.book, note="New revision", origin="test"); db.add(new); await db.flush()
            await rules.publish(db, new.id, old.id); await db.commit()
        self.assertEqual((await self.job(first[0]["id"])).context["rules_version"], first[0]["rules_version"])
        with self.assertRaises(catalog.CatalogError):
            await self.create(request.model_copy(update={"research": "official"}))

    async def test_human_review_does_not_import_until_confirmed(self):
        id = (await self.create())[0]["id"]
        await self.act(id, "start"); await worker.cycle()
        job = await self.job(id)
        self.assertEqual(job.status, "review")
        self.assertFalse(self.clients["biketrek"].sent)
        async with self.sessions() as db:
            job = await service.get_job(db, id, True)
            request = ContentReview(expected_revision=job.revision, content=job.output, approve=True)
            await service.review(db, job, request); await db.commit()
        await worker.cycle()
        self.assertEqual((await self.job(id)).status, "ready")
        self.assertFalse(self.clients["biketrek"].sent)
        await self.act(id, "import"); await worker.cycle()
        self.assertEqual((await self.job(id)).status, "completed")
        sent = [body for path, body in self.clients["biketrek"].sent if path == "products"]
        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0]["products"][0]["active_yn"])
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM stock_movements")), 0)

    async def test_two_shops_auto_policy_keeps_independent_results(self):
        policy = {"review_required": False, "active_after_import": True, "show_cost_estimate": False, "confirm_import": False}
        jobs = await self.create(self.request(targets=[{"shop": s, "policy": policy} for s in ("biketrek", "xtrek")]))
        self.clients["xtrek"].accept = False
        for _ in range(6):
            await worker.cycle()
        results = {j["shop"]: await self.job(j["id"]) for j in jobs}
        self.assertEqual(results["biketrek"].status, "completed")
        self.assertEqual(results["xtrek"].status, "import_failed")
        self.assertTrue(next(iter(self.clients["biketrek"].products.values()))["active_yn"])
        self.assertEqual(self.generate.await_count, 2)
        await worker.cycle()
        self.assertEqual(len([p for p, _ in self.clients["biketrek"].sent if p == "products"]), 1)

    async def test_restart_never_repeats_unknown_paid_request(self):
        id = (await self.create())[0]["id"]
        await self.act(id, "start")
        async with self.sessions() as db:
            job = await service.get_job(db, id, True)
            service.event(job, "generating", "Simulated crash after send"); await db.commit()
        await worker.cycle()
        job = await self.job(id)
        self.assertEqual(job.status, "uncertain")
        self.assertGreater(job.reserved_usd, 0)
        self.generate.assert_not_called()

    async def test_budget_reserved_once_and_cancel_releases(self):
        id = (await self.create())[0]["id"]
        with patch.object(settings, "AI_CONTENT_MONTHLY_USD", 0):
            with self.assertRaises(catalog.CatalogError):
                await self.act(id, "start")
        await self.act(id, "start")
        amount = (await self.job(id)).reserved_usd
        await self.act(id, "start")
        self.assertEqual((await self.job(id)).reserved_usd, amount)
        await self.act(id, "cancel")
        self.assertEqual((await self.job(id)).reserved_usd, 0)
        await worker.cycle(); self.generate.assert_not_called()

    async def test_rule_publish_compare_and_swap(self):
        async with self.sessions() as db:
            old = await rules.published(db)
            next = AiRuleVersion(book=old.book, note="draft", origin="test"); db.add(next); await db.flush()
            await rules.publish(db, next.id, old.id)
            with self.assertRaises(catalog.CatalogError):
                await rules.publish(db, old.id, old.id)
            self.assertEqual((await rules.published(db)).id, next.id)
