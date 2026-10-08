"""New draft policy automation retains approval, edit and publication fences."""
import unittest
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_ai_content import content, context
from test_product_import import document, row
from inventory_hub.catalog_types import ShopImportOptions
from inventory_hub.services import ai_content, ai_content_worker as worker, catalog_import, product_import as service
from inventory_hub.services.catalog import CatalogError


class DraftAutomationTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, *, paused=False, version=1, confirm=False):
        item = row(ai_enabled=True)
        item['ai_job_id'] = 'job'
        ctx = context(staging_id='draft', staging_automation_version=version, approval='policy', rules_version=15,
            shop='biketrek', supplier='paul-lange', product_ids=[1])
        ctx['resolved']['policy'] = dict(review_required=False, show_cost_estimate=False,
            confirm_import=confirm, active_after_import=True)
        job = SimpleNamespace(id='job', context=ctx, output=content().model_dump(), usage={}, revision=3,
            status='preparing_import', events=[], error=None, preview_id=None)
        draft = SimpleNamespace(id='draft', revision=2, supplier='paul-lange', shop='biketrek', status='draft',
            document={**document([item]), 'automation': {'version': 1, 'job_ids': ['job'], 'paused': paused,
                'auto_publish_requested': not confirm}})
        return draft, job

    async def advance(self, draft, job, *, items=None, existing=None, other_jobs=None):
        jobs = [job, *(other_jobs or [])]
        async def scalars(statement):
            ids = next(v for v in statement.compile().params.values() if isinstance(v, list))
            return SimpleNamespace(all=lambda: [j for j in jobs if j.id in ids])
        db = SimpleNamespace(scalars=AsyncMock(side_effect=scalars), flush=AsyncMock())
        async def save(db, current, request):
            current.status = 'saved'
            current.document['saved_hash'] = service.content_hash(current.document)
        async def preview(db, current, request):
            current.document['publication'] = {'preview_id': 'preview', 'errors': [],
                'items': items or [{'status': 'ready', 'product_ids': [1]}]}
        with patch.object(service, 'get_draft', AsyncMock(return_value=draft)), \
             patch.object(service, 'publication_result', return_value=existing), \
             patch.object(service, 'public', AsyncMock(return_value={})), \
             patch.object(service, 'save', AsyncMock(side_effect=save)) as saved, \
             patch.object(service, 'preview', AsyncMock(side_effect=preview)) as prepared:
            await service.advance_ai(db, job)
        return saved, prepared

    async def test_all_auto_applies_saves_and_authorizes_existing_importer_once(self):
        draft, job = self.fixture()
        saved, prepared = await self.advance(draft, job)
        self.assertEqual(job.status, 'import_queued')
        self.assertTrue(service.applied_ai(job))
        self.assertEqual(job.context['staging_auto_publication'], {'draft_id': 'draft',
            'preview_id': 'preview', 'hash': service.content_hash(draft.document)})
        self.assertEqual(draft.document['rows'][0]['values']['description_html'], content().long_description)
        self.assertTrue(service.approved_visibility(draft.document['rows'], [1]))
        saved.assert_awaited_once()
        prepared.assert_awaited_once()

    async def test_confirmation_override_applies_content_but_waits_for_publication(self):
        draft, job = self.fixture(confirm=True)
        saved, prepared = await self.advance(draft, job)
        self.assertEqual(job.status, 'completed')
        self.assertTrue(service.applied_ai(job))
        saved.assert_not_awaited()
        prepared.assert_not_awaited()

    async def test_old_paused_mixed_and_unapproved_drafts_do_not_auto_publish(self):
        for mode in ('old', 'paused', 'mixed', 'other_old', 'unapproved', 'existing_result'):
            with self.subTest(mode=mode):
                draft, job = self.fixture(version=0 if mode == 'old' else 1, paused=mode == 'paused')
                others = []
                if mode == 'mixed':
                    draft.document['rows'].append(row(2, ai_enabled=False))
                if mode == 'other_old':
                    extra = row(2, ai_enabled=True)
                    extra['ai_job_id'] = 'old-job'
                    draft.document['rows'].append(extra)
                    draft.document['automation']['job_ids'].append('old-job')
                    others = [SimpleNamespace(id='old-job', status='completed', context={'staging_id':'draft'})]
                if mode == 'unapproved':
                    job.context['approval'] = None
                saved, prepared = await self.advance(draft, job, other_jobs=others,
                    existing={'status': 'failed', 'items': []} if mode == 'existing_result' else None)
                self.assertNotEqual(job.status, 'import_queued')
                saved.assert_not_awaited()
                prepared.assert_not_awaited()
                if mode in ('old', 'paused', 'unapproved', 'existing_result'):
                    self.assertNotIn('staging_applied_digest', job.context)

    async def test_invalid_family_blocks_entire_automatic_preview(self):
        draft, job = self.fixture()
        await self.advance(draft, job, items=[{'status': 'ready'}, {'status': 'invalid', 'errors': ['missing_price']}])
        self.assertEqual(job.status, 'import_blocked')
        self.assertEqual(job.error, 'preview_invalid')
        self.assertNotIn('staging_auto_publication', job.context)

    async def test_reopening_applied_content_revokes_publication_readiness(self):
        draft, job = self.fixture()
        await self.advance(draft, job)
        self.assertTrue(service.applied_ai(job))
        for status in ('review', 'ready', 'blocked', 'preparing_import'):
            job.status = status
            self.assertFalse(service.applied_ai(job), status)

    async def test_auto_queue_uses_staging_authorization_and_never_retry_failed(self):
        draft, job = self.fixture()
        await self.advance(draft, job)
        db = SimpleNamespace()
        @asynccontextmanager
        async def session():
            yield db
        with patch.object(worker, 'get_session_context', session), \
             patch.object(ai_content, 'get_job', AsyncMock(return_value=job)), \
             patch.object(service, 'get_draft', AsyncMock(return_value=draft)), \
             patch.object(service, 'assert_ai_applied', AsyncMock()) as applied, \
             patch.object(service, 'publication_result', return_value=None), \
             patch.object(catalog_import, 'queue_import', return_value=({}, True)) as queue, \
             patch.object(catalog_import, 'execute_import', AsyncMock()) as execute, \
             patch.object(catalog_import, 'import_result', return_value={'status':'completed','items':[{'status':'created'}]}):
            await worker.importing(job.id)
            await worker.importing(job.id)
        queue.assert_called_once_with('biketrek', 'preview', False, staging_id='draft')
        execute.assert_awaited_once()
        applied.assert_awaited_once()
        self.assertEqual(job.status, 'completed')

    async def test_first_queue_rechecks_withdrawn_other_job_approval(self):
        draft, job = self.fixture()
        await self.advance(draft, job)
        @asynccontextmanager
        async def session():
            yield SimpleNamespace()
        with patch.object(worker, 'get_session_context', session), \
             patch.object(ai_content, 'get_job', AsyncMock(return_value=job)), \
             patch.object(service, 'get_draft', AsyncMock(return_value=draft)), \
             patch.object(service, 'assert_ai_applied', AsyncMock(side_effect=CatalogError('import_ai_not_applied', 'Reopened', 409))), \
             patch.object(service, 'publication_result', return_value=None), \
             patch.object(catalog_import, 'queue_import') as queue:
            await worker.importing(job.id)
        self.assertEqual(job.status, 'import_failed')
        self.assertEqual(job.error, 'import_ai_not_applied')
        queue.assert_not_called()

    async def test_visibility_requires_enabled_same_job_and_profile_and_is_hashed(self):
        draft, job = self.fixture()
        await self.advance(draft, job)
        source = deepcopy(draft.document['rows'][0])
        initial_hash = service.content_hash(draft.document)
        for mode in ('approved', 'disabled', 'other_job', 'other_profile'):
            with self.subTest(mode=mode):
                item = deepcopy(source)
                if mode == 'disabled':
                    item['values']['ai_enabled'] = False
                if mode == 'other_job':
                    item['ai_job_id'] = 'new-job'
                if mode == 'other_profile':
                    item['values']['ai_category_profile'] = 'inner_tubes'
                payload = catalog_import.build_item([service.to_source(item)], ShopImportOptions(), {}, False)
                service.overlay_payload(payload, [item], 'sk', draft.document['categories'])
                active = mode == 'approved'
                self.assertEqual(payload.payload['active_yn'], active)
                self.assertEqual(service.approved_visibility([item], [1]), active)
                self.assertEqual(next(m['value'] for m in payload.payload['metas'] if m['key'] == 'validation_required'), '0' if active else '1')
                catalog_import._assert_payload(payload.payload, expected_active=active)
        draft.document['rows'][0]['ai_policy']['active_after_import'] = False
        self.assertNotEqual(service.content_hash(draft.document), initial_hash)


if __name__ == '__main__':
    unittest.main()
