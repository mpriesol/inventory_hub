"""Exact selected evidence, no recovery writes, bounded bodies and private GET."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import test_product_consolidation_db as fixture
import test_product_dependency_scope_db as scope_fixture
from inventory_hub import config_io
from inventory_hub.ai_content_models import AiJob
from inventory_hub.database import get_session
from inventory_hub.product_import_models import ProductImportDraft
from inventory_hub.routers import product_consolidation as routes
from inventory_hub.services import ai_content, catalog_import, product_import
from inventory_hub.services import product_dependency_evidence as evidence
from inventory_hub.services import product_links as links
from inventory_hub.settings import settings


class EvidenceBoundaryTests(unittest.TestCase):
    def test_credentials_are_not_returned_as_full_raw_evidence(self):
        for value in ({'api_key': 'PRIVATE-SECRET'}, {'nested': {'password': 'PRIVATE-SECRET'}},
                      {'nested': {'api_token': 'PRIVATE-SECRET', 'auth_token': 'PRIVATE-SECRET'}},
                      {'url': 'https://user:PRIVATE-SECRET@example.test/item'},
                      {'url': 'https://example.test/item?access_token=PRIVATE-SECRET'},
                      {'url': 'https://example.test/item?api_token=PRIVATE-SECRET'},
                      {'source_html': '<a href="https://example.test/item?a=1&amp;token=PRIVATE-SECRET">source</a>'},
                      {'key': '-----BEGIN RSA PRIVATE KEY-----PRIVATE-SECRET'}):
            with self.subTest(value=value), self.assertRaisesRegex(evidence.Incomplete, 'sensitive_document_not_returned'):
                evidence._document(json.dumps(value))
        value = {'facts': [{'code': 'SYN-LEAF'}], 'output_tokens': 123, 'future_field': {'identity': 123}}
        self.assertEqual(evidence._document(json.dumps(value)), value)

    def test_symlink_ancestor_or_file_is_never_followed_and_no_directory_is_created(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as outside, \
             patch.object(config_io, 'DATA_ROOT', Path(root)):
            identifier = 'a' * 32
            self.assertIsNone(evidence._file('synthetic', identifier, '.json'))
            self.assertEqual(list(Path(root).iterdir()), [])
            directory = Path(root) / 'shops' / 'synthetic'; directory.mkdir(parents=True)
            (Path(outside) / (identifier + '.json')).write_text('{"private":"OUTSIDE"}')
            (directory / 'catalog-imports').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(OSError):
                evidence._file('synthetic', identifier, '.json')
            (directory / 'catalog-imports').unlink(); (directory / 'catalog-imports').mkdir()
            (directory / 'catalog-imports' / (identifier + '.json')).symlink_to(Path(outside) / (identifier + '.json'))
            with self.assertRaises(OSError):
                evidence._file('synthetic', identifier, '.json')

    def test_operator_no_store_and_bounded_exact_ids_are_checked_before_service(self):
        calls = []
        async def session():
            calls.append(True); yield SimpleNamespace()
        app = FastAPI(); app.include_router(routes.router)
        app.dependency_overrides[get_session] = session
        token = 'synthetic-evidence-' + 'x' * 24
        with TestClient(app) as client, patch.object(settings, 'AI_CONTENT_ACCESS_TOKEN', SecretStr(token)), \
             patch.object(evidence, 'inspect', AsyncMock(return_value={'read_only': True})) as inspect:
            url = '/product-consolidations/' + str(uuid4()) + '/dependency-evidence'
            params = [('job_ids', 'a' * 32)]
            self.assertEqual(client.get(url, params=params).status_code, 401)
            self.assertEqual(calls, []); inspect.assert_not_awaited()
            headers = {'Authorization': 'Bearer ' + token}
            good = client.get(url, params=params, headers=headers)
            self.assertEqual(good.status_code, 200)
            self.assertEqual(good.headers['cache-control'], 'no-store')
            inspect.assert_awaited_once(); inspect.reset_mock()
            for invalid in ([], [('job_ids', 'PRIVATE-NOT-AN-ID')], [('job_ids', 'b' * 32)] * 9):
                response = client.get(url, params=invalid, headers=headers)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.headers['cache-control'], 'no-store')
                self.assertNotIn('PRIVATE-NOT-AN-ID', response.text)
                inspect.assert_not_awaited()


@unittest.skipUnless(fixture.TEST_URL, 'Dedicated localhost *_catalog_test database required')
class DependencyEvidenceDatabaseTests(unittest.IsolatedAsyncioTestCase):
    transaction = fixture.ProductConsolidationDatabaseTests.transaction
    leaf = fixture.ProductConsolidationDatabaseTests.leaf
    discover = fixture.ProductConsolidationDatabaseTests.discover
    verify = fixture.ProductConsolidationDatabaseTests.verify
    absence = fixture.ProductConsolidationDatabaseTests.absence
    code_absence = fixture.ProductConsolidationDatabaseTests.code_absence
    request = fixture.ProductConsolidationDatabaseTests.request
    preview = fixture.ProductConsolidationDatabaseTests.preview
    stock = fixture.ProductConsolidationDatabaseTests.stock
    add_draft = scope_fixture.ProductDependencyScopeDatabaseTests.add_draft
    add_ai = scope_fixture.ProductDependencyScopeDatabaseTests.add_ai
    journal = scope_fixture.ProductDependencyScopeDatabaseTests.journal

    async def asyncSetUp(self):
        await scope_fixture.ProductDependencyScopeDatabaseTests.asyncSetUp(self)
        self.consolidation_id = (await self.preview())['id']

    async def asyncTearDown(self):
        await scope_fixture.ProductDependencyScopeDatabaseTests.asyncTearDown(self)

    async def read(self, ids):
        async with self.transaction() as db:
            return await evidence.inspect(db, self.consolidation_id, ids)

    async def hashes(self):
        async with self.transaction() as db:
            return {table: await db.scalar(text(f'''SELECT md5(coalesce(
                string_agg(to_jsonb(t)::text,E'\\n' ORDER BY to_jsonb(t)::text),'')) FROM {table} t'''))
                for table in ('ai_content_jobs', 'product_import_drafts', 'product_consolidations', 'products', 'stock_balances')}

    async def test_replaced_uncertain_job_full_draft_and_old_queued_sidecars_are_read_without_writes(self):
        draft_id, job_id, _ = await self.add_ai()
        replacement = uuid4().hex
        path, base = self.journal(state='ready', attempted=False)
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        base['staging'] = {'draft_id': draft_id, 'hash': 'a' * 64}
        base['result']['status'] = 'queued'; base['result']['updated_at'] = old
        path.write_text(json.dumps(base))
        path.with_suffix('.result.json').write_text(json.dumps(base['result']))
        event = {'code': base['result']['items'][0]['code'], 'updated_at': datetime.now(timezone.utc).isoformat(),
            'status': 'uncertain', 'product_write_attempted': True, 'errors': [], 'warnings': []}
        path.with_suffix('.events.jsonl').write_text(json.dumps(event) + '\n')
        async with self.transaction() as db:
            job = await db.get(AiJob, job_id); job.status = 'uncertain'; job.error = 'ai_outcome_unknown'
            job.context = {**job.context, 'future_field': {'retain_exactly': [False, None, 123]}}
            draft = await db.get(ProductImportDraft, draft_id); doc = deepcopy(draft.document)
            doc['rows'][0]['ai_job_id'] = replacement
            doc['automation'] = {'version': 1, 'job_ids': [replacement], 'paused': False}
            doc['publication'] = {'preview_id': path.stem}
            draft.document = doc
        unrelated, _, _ = await self.add_ai()
        before, stock = await self.hashes(), await self.stock()
        files = {p.name: p.read_bytes() for p in path.parent.iterdir()}
        with patch.object(ai_content, 'summaries', side_effect=AssertionError('public reader forbidden')), \
             patch.object(product_import, 'public', side_effect=AssertionError('public reader forbidden')), \
             patch.object(catalog_import, 'import_result', side_effect=AssertionError('recovery forbidden')), \
             patch.object(catalog_import, '_write', side_effect=AssertionError('filesystem writes forbidden')), \
             patch.object(config_io, 'load_supplier', side_effect=AssertionError('config forbidden')):
            report = await self.read([job_id])
        self.assertTrue(report['complete'], report['problems'])
        self.assertEqual([r['id'] for r in report['jobs']], [job_id])
        self.assertEqual([r['id'] for r in report['drafts']], [draft_id])
        self.assertNotIn(unrelated, json.dumps(report))
        raw = report['jobs'][0]
        self.assertEqual(raw['sha256'], hashlib.sha256(raw['raw_json'].encode()).hexdigest())
        self.assertEqual(json.loads(raw['raw_json'])['context']['future_field'], {'retain_exactly': [False, None, 123]})
        self.assertEqual(report['selected_job_backlinks'][0]['current_row_ids'], [])
        self.assertFalse(report['selected_job_backlinks'][0]['automation_job_member'])
        observed = report['publication_journals'][0]['files']
        self.assertEqual(json.loads(observed['.result.json']['raw_utf8'])['status'], 'queued')
        self.assertEqual(json.loads(observed['.events.jsonl']['raw_utf8'])['status'], 'uncertain')
        self.assertEqual(files, {p.name: p.read_bytes() for p in path.parent.iterdir()})
        self.assertEqual(await self.hashes(), before); self.assertEqual(await self.stock(), stock)

    async def test_repeatable_read_keeps_original_backlinks_and_database_itself_rejects_writes(self):
        draft_id, job_id, _ = await self.add_ai()
        original, changed = evidence._rows, False
        async def alter_after_jobs(db, table, ids, budget):
            nonlocal changed
            value = await original(db, table, ids, budget)
            if table == 'ai_content_jobs' and not changed:
                changed = True
                async with self.transaction() as other:
                    draft = await other.get(ProductImportDraft, draft_id); doc = deepcopy(draft.document)
                    doc['rows'][0]['ai_job_id'] = uuid4().hex
                    draft.document = doc; draft.revision += 1
            return value
        with patch.object(evidence, '_rows', side_effect=alter_after_jobs):
            async with self.transaction() as db:
                report = await evidence.inspect(db, self.consolidation_id, [job_id])
                self.assertTrue(report['complete'], report['problems'])
                self.assertEqual(report['selected_job_backlinks'][0]['current_row_ids'], [700])
                self.assertEqual(json.loads(report['drafts'][0]['raw_json'])['revision'], 1)
                self.assertEqual(await db.scalar(text('SHOW transaction_read_only')), 'on')
                self.assertEqual(await db.scalar(text('SHOW transaction_isolation')), 'repeatable read')
                with self.assertRaises(DBAPIError) as caught:
                    await db.execute(text("UPDATE products SET name='FORBIDDEN' WHERE id=:id"), {'id': self.keeper})
                self.assertEqual(getattr(caught.exception.orig, 'sqlstate', None), '25006')
                await db.rollback()

    async def test_missing_rows_or_budget_limits_are_explicit_without_silent_partial_bodies(self):
        _, job_id, _ = await self.add_ai()
        missing = await self.read([uuid4().hex])
        self.assertFalse(missing['complete']); self.assertEqual(missing['problems'], ['selected_ai_job_missing'])
        with patch.object(evidence, 'MAX_DOCUMENT_BYTES', 20):
            limited = await self.read([job_id])
        self.assertFalse(limited['complete']); self.assertEqual(limited['jobs'], [])
        self.assertEqual(limited['problems'], ['document_byte_limit'])
        with patch.object(evidence, 'MAX_TOTAL_BYTES', 100):
            limited = await self.read([job_id])
        self.assertEqual(limited['problems'], ['total_evidence_byte_limit'])
        with patch.object(evidence, 'MAX_RESPONSE_BYTES', 100):
            limited = await self.read([job_id])
        self.assertFalse(limited['complete']); self.assertEqual(limited['drafts'], [])
        self.assertEqual(limited['problems'], ['evidence_response_byte_limit'])
        with self.assertRaises(links.LinkError) as caught:
            await self.read([job_id, job_id])
        self.assertEqual(caught.exception.status, 422)
        async with self.transaction() as db:
            job = await db.get(AiJob, job_id); job.preview_id = ''
        invalid = await self.read([job_id])
        self.assertFalse(invalid['complete']); self.assertEqual(invalid['problems'], ['invalid_linked_identifier'])

    async def test_sensitive_database_document_is_not_echoed(self):
        _, job_id, _ = await self.add_ai()
        async with self.transaction() as db:
            job = await db.get(AiJob, job_id)
            job.context = {**job.context, 'credentials': {'value': 'PRIVATE-CREDENTIAL'}}
        report = await self.read([job_id])
        self.assertFalse(report['complete']); self.assertEqual(report['jobs'], [])
        self.assertEqual(report['problems'], ['sensitive_document_not_returned'])
        self.assertNotIn('PRIVATE-CREDENTIAL', json.dumps(report))

    async def test_authoritative_multishop_targets_and_transitive_staging_draft_are_complete(self):
        draft_id, job_id, _ = await self.add_ai()
        next_id, next_doc = await self.add_draft()
        first, base = self.journal(); second, _ = self.journal()
        # Same shop is valid for separate journal sets; the aggregate is not a file.
        aggregate = uuid4().hex
        async with self.transaction() as db:
            draft = await db.get(ProductImportDraft, draft_id); doc = deepcopy(draft.document)
            doc['publications'] = {'biketrek': {'preview_id': first.stem}, 'xtrek': {'preview_id': second.stem}}
            doc['publication'] = {'preview_id': aggregate}
            draft.document = doc
            job = await db.get(AiJob, job_id); job.preview_id = aggregate
            job.context = {**job.context, 'staging_auto_publication': {'draft_id': draft_id, 'preview_id': aggregate}}
        base['staging'] = {'draft_id': next_id}; first.write_text(json.dumps(base))
        xtrek = config_io.shop_path('xtrek').parent / 'catalog-imports' / second.name
        xtrek.parent.mkdir(parents=True, exist_ok=True); second.replace(xtrek)
        report = await self.read([job_id])
        self.assertTrue(report['complete'], report['problems'])
        self.assertEqual({r['id'] for r in report['drafts']}, {draft_id, next_id})
        self.assertEqual({r['journal_id'] for r in report['publication_journals']}, {first.stem, xtrek.stem})
        self.assertNotIn(aggregate, {r['journal_id'] for r in report['publication_journals']})

    async def test_changed_sidecar_and_incomplete_tail_never_claim_complete_evidence(self):
        draft_id, job_id, _ = await self.add_ai()
        path, base = self.journal()
        async with self.transaction() as db:
            draft = await db.get(ProductImportDraft, draft_id); doc = deepcopy(draft.document)
            doc['publication'] = {'preview_id': path.stem}; draft.document = doc
        original, reads = evidence._file, 0
        def change_result(shop, identifier, suffix):
            nonlocal reads
            if suffix == '.json':
                reads += 1
                if reads == 2:
                    path.with_suffix('.result.json').write_text(json.dumps({'status': 'uncertain'}))
            return original(shop, identifier, suffix)
        with patch.object(evidence, '_file', side_effect=change_result):
            report = await self.read([job_id])
        self.assertFalse(report['complete']); self.assertEqual(report['problems'], ['journal_changed_between_observations'])
        path.with_suffix('.events.jsonl').write_text('{"status":"uncertain"')
        report = await self.read([job_id])
        self.assertFalse(report['complete']); self.assertEqual(report['problems'], ['journal_event_incomplete_line'])
