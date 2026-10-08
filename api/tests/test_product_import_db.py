"""Durable import staging against an isolated PostgreSQL schema and fake shops."""
import json
import unittest
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from pydantic import SecretStr
from sqlalchemy import select, func, text

import test_catalog_db as catalog_db
from catalog_fixtures import FakeUpgates, xml_item
from test_ai_content import content
from test_ai_content_db import FakeContentShop
from inventory_hub import config_io, database
from inventory_hub.ai_content_models import AiJob
from inventory_hub.db_models import Product
from inventory_hub.db_models_ext import ShopProduct, ProductSupplySource
from inventory_hub.product_import_models import ProductImportDraft
from inventory_hub.product_import_types import DraftCreate, DraftPatch, DraftRevision, DraftAiRequest, DraftPublish
from inventory_hub.services import product_import as service, catalog, catalog_import, ai_content_worker as worker
from inventory_hub.settings import settings


@unittest.skipUnless(catalog_db.TEST_URL, 'Dedicated localhost *_catalog_test database required')
class ProductImportDatabaseTests(unittest.IsolatedAsyncioTestCase):
    write_feed = catalog_db.CatalogDatabaseTests.write_feed
    refresh = catalog_db.CatalogDatabaseTests.refresh

    async def asyncSetUp(self):
        await catalog_db.CatalogDatabaseTests.asyncSetUp(self)
        async with self.engine.begin() as connection:
            raw = await connection.get_raw_connection()
            for name in ('005_ai_content.sql', '012_product_editor.sql', '021_product_feed_mapping.sql', '022_product_import_drafts.sql', '023_ai_content_settings.sql'):
                sql = (Path(__file__).resolve().parents[2] / 'infra/db-init' / name).read_text()
                await raw.driver_connection.execute(sql)
                if name == '022_product_import_drafts.sql':
                    await raw.driver_connection.execute(sql)
        await self.refresh()
        self.client = FakeContentShop()
        path = config_io.shop_path('biketrek')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'upgates_api_base_url':'https://biketrek.example.test/api/v2',
                                   'upgates_login':'fixture', 'upgates_api_key':'fixture'}))
        @asynccontextmanager
        async def sessions():
            async with self.sessions() as db:
                yield db
                await db.commit()
        self.patches = [patch.object(catalog_import.UpgatesClient, 'from_shop', return_value=self.client),
            patch.object(catalog_import, 'get_session_context', sessions), patch.object(worker, 'get_session_context', sessions),
            patch.object(database, '_engine', self.engine), patch.object(settings, 'AI_CONTENT_ENABLED', True),
            patch.object(settings, 'OPENAI_API_KEY', SecretStr('fixture-no-network')), patch.object(settings, 'AI_CONTENT_MONTHLY_USD', 20)]
        for p in self.patches:
            p.start()
        async with self.sessions() as db:
            page = await catalog.catalog_page(db, 'paul-lange')
            self.ids = [item.product.id for item in page.items]

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()
        await catalog_db.CatalogDatabaseTests.asyncTearDown(self)

    def request(self, **changes):
        return DraftCreate(request_id=uuid4(), supplier='paul-lange', shop='biketrek',
            product_ids=[self.ids[0]], options={'category_code':'K-TEST'}, **changes)

    async def create(self, request=None):
        async with self.sessions() as db:
            result = await service.create(db, request or self.request())
            await db.commit()
            return result

    async def mutate(self, result, fn, request_type=DraftRevision, **kwargs):
        async with self.sessions() as db:
            draft = await service.get_draft(db, result['id'], lock=True)
            response = await fn(db, draft, request_type(expected_revision=result['revision'], **kwargs))
            await db.commit()
            return response

    async def test_draft_snapshot_holds_supplier_mapping_lock_until_commit(self):
        async with self.sessions() as writer:
            result = await service.create(writer, self.request())
            async with self.sessions() as competing:
                available = await competing.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key':catalog._lock_key('paul-lange')})
                self.assertFalse(available, 'Mapping save or feed refresh must not interleave draft rows')
                await competing.rollback()
            await writer.commit()
        async with self.sessions() as competing:
            available = await competing.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key':catalog._lock_key('paul-lange')})
            self.assertTrue(available)
            await competing.rollback()

    async def test_snapshot_replay_cas_and_hub_save_never_create_shop_or_stock(self):
        request = self.request()
        result = await self.create(request)
        replay = await self.create(request)
        self.assertEqual(result['id'], replay['id'])
        result = await self.mutate(result, service.patch_rows, DraftPatch,
            rows=[{'id': self.ids[0], 'values': {'name':'Ručne zmenený názov', 'sale_gross':'89.99'}}])
        with self.assertRaises(catalog.CatalogError) as error:
            await self.mutate(replay, service.save)
        self.assertEqual(error.exception.code, 'import_draft_changed')
        saved = await self.mutate(result, service.save)
        self.assertEqual(saved['status'], 'saved')
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(Product)), 1)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ShopProduct)), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductSupplySource)), 1)
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM stock_balances')), 0)
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM stock_movements')), 0)
        self.assertEqual(self.client.sent, [])
        # Save replay through a current revision reuses the same canonical product.
        saved2 = await self.mutate(saved, service.save)
        self.assertEqual(saved['rows'][0]['hub_product_id'], saved2['rows'][0]['hub_product_id'])

    async def test_explicit_publication_uses_edited_snapshot_after_feed_refresh(self):
        result = await self.create()
        result = await self.mutate(result, service.patch_rows, DraftPatch, rows=[{'id':self.ids[0], 'values': {
            'name':'Môj názov', 'description_html':'<p>Ručný popis</p>', 'seo_title':'Moje SEO',
            'seo_description':'Presný popis z tabuľky', 'brand':'Nová značka', 'sale_gross':'89.99',
            'images':['https://images.example.com/edited.jpg'], 'parameters':[{'name':'Farba','value':'modrá'}]}}])
        result = await self.mutate(result, service.save)
        self.write_feed(xml_item(name='Updated supplier name'))
        await self.refresh()
        result = await self.mutate(result, service.preview)
        preview = result['publication']
        payload = preview['items'][0]['payload']
        self.assertEqual(payload['descriptions'][0]['title'], 'Môj názov')
        self.assertEqual(payload['manufacturer'], 'Nová značka')
        self.assertEqual(payload['images'][0]['url'], 'https://images.example.com/edited.jpg')
        self.assertEqual(payload['parameters'][0]['values'][0]['descriptions'][0]['value'], 'modrá')
        with self.assertRaises(catalog.CatalogError) as error:
            catalog_import.queue_import('biketrek', preview['preview_id'])
        self.assertEqual(error.exception.code, 'import_staging_confirmation_required')
        published, run = await self.mutate(result, service.publish, DraftPublish, preview_id=preview['preview_id'])
        self.assertTrue(run)
        await catalog_import.execute_import('biketrek', preview['preview_id'])
        outcome = catalog_import.import_result('biketrek', preview['preview_id'])
        self.assertEqual(outcome['items'][0]['status'], 'created', outcome)
        self.assertEqual(len([sent for sent in self.client.sent if sent[0]=='products']), 1)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(ShopProduct)), 1)
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM stock_movements')), 0)

    async def test_saved_existing_product_is_not_overwritten(self):
        first = await self.mutate(await self.create(), service.save)
        second = await self.create()
        second = await self.mutate(second, service.patch_rows, DraftPatch, rows=[{'id':self.ids[0], 'values':{'name':'Should not replace existing'}}])
        saved = await self.mutate(second, service.save)
        self.assertIn('already_in_hub_unchanged', saved['rows'][0]['warnings'])
        self.assertEqual(saved['rows'][0]['hub_product_id'], first['rows'][0]['hub_product_id'])
        from inventory_hub.product_editor_models import ProductEditorOverride
        async with self.sessions() as db:
            override = await db.get(ProductEditorOverride, first['rows'][0]['hub_product_id'])
            self.assertNotEqual(override.data['common']['name'], 'Should not replace existing')

    async def test_ai_reads_mapped_edited_facts_and_preserves_later_manual_work(self):
        result = await self.create()
        result = await self.mutate(result, service.patch_rows, DraftPatch, rows=[{'id':self.ids[0], 'values':{
            'description_html':'<p>Mapped technical fact</p>', 'ai_enabled':True, 'ai_category_profile':'general'}}])
        result = await self.mutate(result, service.prepare_ai, DraftAiRequest, research='feed_only')
        job_id = result['rows'][0]['ai_job']['id']
        async with self.sessions() as db:
            job = await db.get(AiJob, job_id)
            self.assertIn('Mapped technical fact', job.context['facts'][0]['description'])
            self.assertEqual(job.status, 'estimate')
            self.assertEqual(job.context['options']['category_code'], 'K-TEST')
        result = await self.mutate(result, service.start_ai)
        result = await self.mutate(result, service.patch_rows, DraftPatch, rows=[{'id':self.ids[0], 'values':{'name':'Edited during AI'}}])
        output = content(evidence=[{'claim':'Mapped technical fact','source':f'feed:{self.ids[0]}','quote':'Mapped technical fact'}])
        response = {'id':'testresponse','status':'completed','usage':{'input_tokens':100,'output_tokens':100},
            'output':[{'type':'message','content':[{'type':'output_text','text':output.model_dump_json()}]}]}
        with patch.object(worker.provider, 'generate', AsyncMock(return_value=response)) as generate:
            await worker.generation(job_id)
            self.assertEqual(generate.call_count, 1)
        result = await self.mutate(result, service.save)
        with self.assertRaises(catalog.CatalogError) as pending:
            await self.mutate(result, service.preview)
        self.assertEqual(pending.exception.code, 'import_ai_not_applied')
        applied = await self.mutate(result, service.apply_ai)
        value = applied['rows'][0]['values']
        self.assertEqual(value['name'], 'Edited during AI')
        self.assertEqual(value['description_html'], '<p>Mapped technical fact</p>')
        self.assertEqual(value['seo_title'], output.seo_title)
        self.assertEqual(value['category_code'], 'K-TEST')
        self.assertEqual(self.client.sent, [])
        async with self.sessions() as db:
            job = await db.get(AiJob, job_id)
            self.assertEqual(job.status, 'completed')
            self.assertIsNone(job.preview_id)
        saved = await self.mutate(applied, service.save)
        prepared = await self.mutate(saved, service.preview)
        published, run = await self.mutate(prepared, service.publish, DraftPublish, preview_id=prepared['publication']['preview_id'])
        self.assertTrue(run)
        await catalog_import.execute_import('biketrek', prepared['publication']['preview_id'])
        async with self.sessions() as db:
            from inventory_hub.services import ai_content
            job = await db.get(AiJob, job_id)
            summary = (await ai_content.summaries(db, [job], detail=True))[0]
            self.assertEqual(summary['staging']['publication_status'], 'completed')
            self.assertTrue(summary['staging']['ai_applied'])
            draft = await service.get_draft(db, saved['id'])
            final = await service.public(db, draft)
            self.assertEqual(final['publication_state'], 'completed')
            self.assertEqual(final['rows'][0]['publication_status'], 'created')
            self.assertEqual((await service.history(db))['items'][0]['publication_state'], 'completed')

    async def test_inherited_automatic_policy_creates_one_active_product_without_manual_steps(self):
        from inventory_hub.ai_content_models import AiRuleVersion
        from inventory_hub.ai_content_types import Policy, RuleBook
        from inventory_hub.services import ai_content_rules
        async with self.sessions() as db:
            current = await ai_content_rules.published(db)
            rules = RuleBook.model_validate(current.book)
            rules.rules[0].policy = Policy(review_required=False, active_after_import=True,
                show_cost_estimate=False, confirm_import=False)
            version = AiRuleVersion(book=rules.model_dump(), origin='test', note='Automatic policy inheritance')
            db.add(version)
            await db.flush()
            await ai_content_rules.publish(db, version.id, current.id)
            await db.commit()
        result = await self.create()
        result = await self.mutate(result, service.patch_rows, DraftPatch, rows=[{'id': self.ids[0], 'values': {
            'ai_enabled': True, 'ai_category_profile': 'general'}}])
        result = await self.mutate(result, service.prepare_ai, DraftAiRequest, research='feed_only')
        job_id = result['rows'][0]['ai_job']['id']
        self.assertEqual(result['rows'][0]['ai_job']['status'], 'queued')
        self.assertEqual(result['rows'][0]['ai_job']['policy'], dict(review_required=False, active_after_import=True,
            show_cost_estimate=False, confirm_import=False))
        self.assertNotIn('run', result['rows'][0]['ai_job']['origins'].values())
        output = content(evidence=[{'claim': 'Popis', 'source': f'feed:{self.ids[0]}', 'quote': 'Popis'}])
        response = {'id': 'automatic-fixture', 'status': 'completed', 'usage': {'input_tokens': 100, 'output_tokens': 100},
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': output.model_dump_json()}]}]}
        with patch.object(worker.provider, 'generate', AsyncMock(return_value=response)) as generate:
            await worker.cycle()  # One paid generation, fake provider.
            await worker.cycle()  # Apply, save, preview and authorize once.
            async with self.sessions() as db:
                pending = await db.get(AiJob, job_id)
                self.assertEqual(pending.status, 'import_queued', pending.error)
                self.assertTrue(service.applied_ai(pending))
            await worker.cycle()  # Existing create-only importer.
            await worker.cycle()  # No duplicate generation or publication.
            self.assertEqual(generate.await_count, 1)
        sent = [payload for path, payload in self.client.sent if path == 'products']
        self.assertEqual(len(sent), 1)
        payload = sent[0]['products'][0]
        self.assertTrue(payload['active_yn'])
        self.assertTrue(payload['descriptions'][0]['active_yn'])
        self.assertEqual(next(m['value'] for m in payload['metas'] if m['key'] == 'validation_required'), '0')
        async with self.sessions() as db:
            job = await db.get(AiJob, job_id)
            self.assertEqual(job.status, 'completed', job.error)
            final = await service.public(db, await service.get_draft(db, result['id']))
            self.assertEqual(final['publication_state'], 'completed')
            self.assertTrue(final['rows'][0]['ai_job']['staging']['ai_applied'])
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM stock_movements')), 0)
