"""New AI state machine against an isolated PostgreSQL schema and fake providers."""
import copy
import json
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from pydantic import SecretStr
from sqlalchemy import select, text

import test_catalog_db as catalog_db
from catalog_fixtures import FakeUpgates
from test_ai_content import content
from inventory_hub import config_io, database
from inventory_hub.ai_content_models import AiContentRevision, AiJob, AiRuleVersion
from inventory_hub.ai_content_types import BatchRequest, ContentReview, JobAction, JobFork, Policy, RuleBook, Target
from inventory_hub.services import ai_content as service, ai_content_rules as rules, ai_content_worker as worker, catalog, catalog_import
from inventory_hub.services import ai_content_settings as model_settings
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
            for filename in ("005_ai_content.sql", "015_stock_sync.sql", "016_product_publication.sql", "018_fifo_cost_sync.sql", "023_ai_content_settings.sql"):
                await raw.driver_connection.execute((Path(__file__).resolve().parents[2] / "infra/db-init" / filename).read_text())
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

    async def test_model_setting_changes_only_new_jobs_and_request_replay_keeps_original(self):
        request = self.request()
        original = (await self.create(request))[0]
        async with self.sessions() as db:
            result = await model_settings.read(db)
            self.assertEqual(result['source'], 'server')
            await model_settings.save(db, model_settings.ModelSettingsSave(expected_revision=0, model='gpt-6.1-sol'))
            await db.commit()
        fresh = (await self.create())[0]
        replay = (await self.create(request))[0]
        self.assertEqual(fresh['model'], 'gpt-6.1-sol')
        self.assertEqual(replay['id'], original['id'])
        self.assertEqual(replay['model'], original['model'])
        self.assertEqual((await self.job(original['id'])).context['model'], original['model'])
        async with self.sessions() as db:
            with self.assertRaises(catalog.CatalogError) as error:
                await model_settings.save(db, model_settings.ModelSettingsSave(expected_revision=0, model='gpt-6-luna'))
            self.assertEqual(error.exception.code, 'ai_settings_changed')

    async def test_model_settings_migration_is_repeatable_and_parallel_initial_saves_use_cas(self):
        import asyncio
        async with self.engine.begin() as connection:
            raw = await connection.get_raw_connection()
            sql = (Path(__file__).resolve().parents[2] / 'infra/db-init/023_ai_content_settings.sql').read_text()
            await raw.driver_connection.execute(sql)
        async def save(model):
            async with self.sessions() as db:
                try:
                    result = await model_settings.save(db, model_settings.ModelSettingsSave(expected_revision=0, model=model))
                    await db.commit()
                    return result['model']
                except catalog.CatalogError as error:
                    return error.code
        results = await asyncio.gather(save('gpt-6.1-sol'), save('gpt-6-luna'))
        self.assertEqual(results.count('ai_settings_changed'), 1)
        async with self.sessions() as db:
            result = await model_settings.read(db)
        self.assertEqual(result['revision'], 1)
        self.assertIn(result['model'], results)

    async def job(self, id):
        async with self.sessions() as db:
            return await service.get_job(db, id)

    async def act(self, id, action):
        async with self.sessions() as db:
            job = await service.get_job(db, id, True)
            await service.action(db, job, JobAction(expected_revision=job.revision, action=action))
            await db.commit()

    async def automatic_category_fixture(self, confident=True):
        from inventory_hub.ai_content_types import CategoryProfile, ParameterDefinition
        async with self.sessions() as db:
            previous = await rules.published(db)
            book = RuleBook.model_validate(previous.book)
            book.categories.append(CategoryProfile(id='helmets', name='Prilby', shop_categories={'biketrek':'ROOT'},
                parameters=[ParameterDefinition(name='Farba')]))
            version = AiRuleVersion(book=book.model_dump(), note='Automatic category fixture', origin='test')
            db.add(version); await db.flush()
            await rules.publish(db, version.id, previous.id); await db.commit()
        client = self.clients['biketrek']
        original_get = client.get
        def get(path, params=None):
            if path == 'categories':
                return {'categories': [
                    {'code':'MENU','category_id':1,'parent_id':None},
                    {'code':'ROOT','category_id':2,'parent_id':1,'descriptions':[{'language':'sk','name':'Prilby'}]},
                    {'code':'LEAF','category_id':3,'parent_id':2,'descriptions':[{'language':'sk','name':'Cestné prilby'}]}]}
            return original_get(path, params)
        patched = patch.object(client, 'get', side_effect=get)
        patched.start(); self.patches.append(patched)
        selection = {'category_code':'LEAF','profile_id':'helmets','confident':confident,'reason':'Typ z feedu'}
        return {**self.response, 'id':'classification_fixture', 'output':[{'type':'message', 'content':[
            {'type':'output_text', 'text':json.dumps(selection)}]}]}

    async def test_automatic_category_uses_registry_and_imports_all_product_ancestors(self):
        classification = await self.automatic_category_fixture()
        self.generate.side_effect = [classification, self.response]
        id = (await self.create(self.request(category_profiles={self.id:'auto'})))[0]['id']
        original = await self.job(id)
        self.assertEqual(original.status, 'estimate')
        from inventory_hub.routers.ai_content import job_request
        async with self.sessions() as db:
            self.assertEqual((await job_request(id, db))['stage'], 'classification')
        self.generate.assert_not_called()
        await self.act(id, 'start'); await worker.cycle()
        job = await self.job(id)
        self.assertEqual(job.status, 'review')
        self.assertEqual(job.context['options']['category_code'], 'LEAF')
        self.assertEqual(job.context['category_profile'], 'helmets')
        self.assertEqual(self.generate.call_args_list[0].args[1], 'classification')
        context = self.generate.call_args_list[1].args[0]
        self.assertEqual(context['resolved']['category']['parameters'][0]['name'], 'Farba')
        self.assertEqual(job.actual_usd, worker.provider.usage_cost(self.response, context['model'])[1] * 2)
        self.assertIn('classification', job.usage)
        self.assertFalse(self.clients['biketrek'].sent)
        async with self.sessions() as db:
            job = await service.get_job(db, id, True)
            await service.review(db, job, ContentReview(expected_revision=job.revision, content=job.output, approve=True))
            await db.commit()
        await worker.cycle(); await self.act(id, 'import'); await worker.cycle()
        self.assertEqual((await self.job(id)).status, 'completed')
        sent = [body for path, body in self.clients['biketrek'].sent if path == 'products']
        self.assertEqual(sent[0]['products'][0]['categories'], [{'code':'ROOT','main_yn':False},{'code':'LEAF','main_yn':True}])

    async def test_uncertain_category_stops_before_content_and_preserves_billed_usage(self):
        self.generate.return_value = await self.automatic_category_fixture(confident=False)
        id = (await self.create(self.request(category_profiles={self.id:'auto'})))[0]['id']
        await self.act(id, 'start'); await worker.cycle()
        job = await self.job(id)
        self.assertEqual((job.status, job.error), ('failed', 'ai_category_uncertain'))
        self.assertIsNone(job.output)
        self.assertGreater(job.actual_usd, 0)
        self.assertEqual(self.generate.await_count, 1)
        self.assertFalse(self.clients['biketrek'].sent)

    async def test_timeout_after_category_keeps_full_reservation_without_paid_retry(self):
        classification = await self.automatic_category_fixture()
        self.generate.side_effect = [classification, catalog.CatalogError('ai_outcome_unknown', 'Synthetic timeout', 502)]
        id = (await self.create(self.request(category_profiles={self.id:'auto'})))[0]['id']
        await self.act(id, 'start'); await worker.cycle()
        job = await self.job(id)
        self.assertEqual(job.status, 'uncertain')
        self.assertGreater(job.actual_usd, 0)
        self.assertGreater(job.reserved_usd, job.actual_usd)
        async with self.sessions() as db:
            self.assertEqual(await service.budget(db), job.reserved_usd)
        await worker.cycle()
        self.assertEqual(self.generate.await_count, 2)
        self.assertFalse(self.clients['biketrek'].sent)

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

    async def test_selected_leaf_survives_profile_default_and_request_is_read_only(self):
        from inventory_hub.ai_content_types import CategoryProfile
        from inventory_hub.routers.ai_content import job_request
        async with self.sessions() as db:
            previous = await rules.published(db)
            book = RuleBook.model_validate(previous.book)
            book.categories.append(CategoryProfile(id="bikes", name="Bikes", shop_categories={"biketrek":"ROOT"}, shop_category_matches={"biketrek":["LEAF"]}))
            version = AiRuleVersion(book=book.model_dump(),note="Fixture registry",origin="test")
            db.add(version); await db.flush()
            await rules.publish(db,version.id,previous.id); await db.commit()
        jobs = await self.create(self.request(targets=[{"shop":"biketrek","options":{"category_code":"LEAF"}}]))
        saved = await self.job(jobs[0]["id"])
        self.assertEqual(saved.context["category_profile"],"bikes")
        self.assertEqual(saved.context["options"]["category_code"],"LEAF")
        async with self.sessions() as db:
            request = await job_request(saved.id,db)
        self.assertEqual(request["rules_version"],saved.context["rules_version"])
        self.assertEqual((await self.job(saved.id)).status,"estimate")
        self.generate.assert_not_called()
        self.assertFalse(self.clients["biketrek"].sent)

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

    async def test_saved_legacy_source_can_be_rechecked_without_generation_or_import(self):
        id = (await self.create())[0]["id"]
        source = "https://manufacturer.example/product"
        output = content().model_dump()
        output["evidence"] = [{"claim": "fact", "quote": "source quote", "source": "official:" + source}]
        async with self.sessions() as db:
            job = await service.get_job(db, id, True)
            job.output, job.usage = output, {"opened_sources": [source]}
            job.actual_usd = Decimal("0.123")
            job.checks = {"errors": ["ai_unverified_official_evidence"]}
            service.event(job, "blocked", "Synthetic legacy result")
            await db.commit()
        async with self.sessions() as db:
            job = await service.get_job(db, id, True)
            request = ContentReview(expected_revision=job.revision, content=job.output, approve=False)
            await service.review(db, job, request)
            await db.commit()
        job = await self.job(id)
        self.assertEqual(job.status, "review")
        self.assertEqual(job.output["evidence"][0]["source"], source)
        self.assertEqual(job.checks["errors"], [])
        self.assertEqual(job.actual_usd, Decimal("0.123"))
        self.assertIsNone(job.preview_id)
        self.generate.assert_not_called()
        self.assertFalse(self.clients["biketrek"].sent)

    async def test_legacy_html_failure_recovers_without_paid_generation_or_import(self):
        id = (await self.create())[0]["id"]
        html = '<div class="specs"><table><tr><th scope="row">Rozmer</th><td colspan="2">50-559</td></tr></table></div>'
        async with self.sessions() as db:
            job = await service.get_job(db, id, True)
            job.output = content(title="TEST - červená prilba", long_description=html,
                evidence=[{"claim": "Popis", "source": f"feed:{self.id}", "quote": "Popis"}]).model_dump()
            job.actual_usd = Decimal("0.123")
            job.checks = {"errors": ["ai_unsafe_html"]}
            service.event(job, "blocked", "Synthetic result blocked by the old HTML whitelist")
            original = copy.deepcopy(job.output)
            await db.commit()
        async with self.sessions() as db:
            job = await service.get_job(db, id, True)
            await service.review(db, job, ContentReview(expected_revision=job.revision, content=job.output, approve=False))
            await db.commit()
        job = await self.job(id)
        self.assertEqual(job.status, "review")
        self.assertEqual(job.checks["errors"], [])
        self.assertEqual(job.output, original)
        self.assertEqual(job.actual_usd, Decimal("0.123"))
        self.assertIsNone(job.preview_id)
        self.assertIsNone(job.context.get("approval"))
        self.generate.assert_not_called()
        self.assertFalse(self.clients["biketrek"].sent)

    async def test_feed_quote_can_be_rechecked_and_corrected_without_ai_or_import(self):
        id = (await self.create())[0]["id"]
        quote = "Model-A, hmotnosť 980 g."
        async with self.sessions() as db:
            job = await service.get_job(db, id, True)
            facts = [{**p, "description": "Model\u2011A, hmotnosť 980 g."} for p in job.context["facts"]]
            job.context = {**job.context, "facts": facts}
            job.output = content(evidence=[{"claim": "Hmotnosť modelu", "source": f"feed:{self.id}", "quote": quote}]).model_dump()
            job.actual_usd = Decimal("0.123")
            job.checks = {"errors": ["ai_unverified_feed_evidence"]}
            service.event(job, "blocked", "Synthetic hyphen mismatch")
            await db.commit()
        for next_quote, expected_status in [(quote, "review"), (quote + " Nepodložená veta.", "blocked"), (quote, "review")]:
            async with self.sessions() as db:
                job = await service.get_job(db, id, True)
                request = ContentReview(expected_revision=job.revision, content={**job.output, "title": "Model — popis"}, approve=False)
                request.content.evidence[0].quote = next_quote
                await service.review(db, job, request)
                await db.commit()
            job = await self.job(id)
            self.assertEqual(job.status, expected_status)
            self.assertEqual(job.output["evidence"][0]["quote"], next_quote)
            self.assertEqual(job.output["title"], "Model - popis")
            self.assertEqual(job.checks["errors"], ["ai_unverified_feed_evidence"] if expected_status == "blocked" else [])
            self.assertEqual(job.context["facts"], facts)
            self.assertEqual(job.actual_usd, Decimal("0.123"))
            self.assertIsNone(job.preview_id)
            self.generate.assert_not_called()
            self.assertFalse(self.clients["biketrek"].sent)

    async def test_manual_approval_prepares_and_imports_with_retained_source_warnings(self):
        id = (await self.create())[0]['id']
        output = content(missing_facts=['Rozpor v dĺžke ventilu'], evidence=[
            {'claim': 'Údaj z feedu', 'source': f'feed:{self.id}', 'quote': 'Nenájdený citát'},
            {'claim': 'Technická tabuľka', 'source': 'https://manufacturer.example/table.pdf', 'quote': 'AV 33 mm'},
        ]).model_dump()
        expected = ['ai_missing_facts', 'ai_unverified_feed_evidence', 'ai_unverified_official_evidence']
        async with self.sessions() as db:
            job = await service.get_job(db, id, True)
            job.output = output
            job.actual_usd = Decimal('0.123')
            service.event(job, 'blocked', 'Stored generation with editorial concerns')
            await db.commit()
        # Saving a draft is not approval, including after an earlier approval.
        for approve in (False, True, False, True):
            async with self.sessions() as db:
                job = await service.get_job(db, id, True)
                if job.status == 'preparing_import':
                    await service.prepare_import(db, job)
                await service.review(db, job, ContentReview(expected_revision=job.revision, content=output, approve=approve))
                await db.commit()
            job = await self.job(id)
            self.assertEqual(job.status, 'preparing_import' if approve else 'blocked')
            self.assertEqual(job.checks['errors'], [] if approve else expected)
            self.assertEqual(job.checks['manual_overrides'], expected if approve else [])
            self.assertEqual(job.context['approval'], 'human' if approve else None)
            self.assertEqual(job.output, output)
            self.assertFalse(self.clients['biketrek'].sent)
        self.assertIn('accepted warnings: ai_missing_facts', job.events[-1]['note'])
        await worker.cycle()
        job = await self.job(id)
        self.assertEqual(job.status, 'ready')
        self.assertTrue(job.preview_id)
        self.assertTrue(set(expected) <= set(job.checks['warnings']))
        await self.act(id, 'import')
        await worker.cycle()
        self.assertEqual((await self.job(id)).status, 'completed')
        sent = [body for path, body in self.clients['biketrek'].sent if path == 'products']
        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0]['products'][0]['active_yn'])
        self.generate.assert_not_called()
        self.assertEqual((await self.job(id)).actual_usd, Decimal('0.123'))
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM stock_movements')), 0)
            revisions = list(await db.scalars(select(AiContentRevision).where(AiContentRevision.job_id == id)))
            self.assertEqual(sum(r.decision == 'human_approved' for r in revisions), 2)

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

    async def test_archiving_retains_paid_usage_and_can_be_restored(self):
        id = (await self.create())[0]['id']
        async with self.sessions() as db:
            job = await service.get_job(db, id, True)
            job.actual_usd = Decimal('0.15')
            service.event(job, 'failed', 'Synthetic paid failure')
            await db.commit()
        await self.act(id, 'archive')
        async with self.sessions() as db:
            job = await service.get_job(db, id)
            self.assertTrue(job.context['archived'])
            self.assertEqual(job.actual_usd, Decimal('0.15'))
            self.assertEqual(await service.budget(db), Decimal('0.15'))
        await self.act(id, 'restore')
        self.assertFalse((await self.job(id)).context['archived'])
        self.generate.assert_not_called()

    async def test_fork_source_revision_prevents_duplicate_batch_on_replayed_request(self):
        id = (await self.create())[0]['id']
        request = JobFork(expected_revision=1, product_ids=[self.id], reuse_content=False)
        async with self.sessions() as db:
            original = await service.get_job(db, id, True)
            fresh = await service.fork_job(db, original, request)
            await db.commit()
        self.assertNotEqual(fresh['id'], id)
        async with self.sessions() as db:
            original = await service.get_job(db, id, True)
            with self.assertRaises(catalog.CatalogError) as error:
                await service.fork_job(db, original, request)
            self.assertEqual(error.exception.code, 'ai_job_changed')
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM ai_content_jobs')), 2)
        self.generate.assert_not_called()
