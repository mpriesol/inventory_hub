"""Writer-based AI scope proofs, including mixed intents and apply-time drift."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError
from sqlalchemy import text

import test_product_consolidation_db as fixture
import test_product_dependency_scope_db as original
from test_ai_content import content
from inventory_hub.ai_content_models import AiBatch, AiJob
from inventory_hub.ai_content_types import Content
from inventory_hub.product_import_models import ProductImportDraft
from inventory_hub.services import ai_content, ai_content_existing, product_import
from inventory_hub.services import product_dependency_ai_scope as ai_scope, product_dependency_scope as scope
from inventory_hub.services import product_links as links
from inventory_hub.services.catalog import CatalogError
from inventory_hub.sloger_probe_types import SlogerObservation


def job(context, *, kind='product', status='uncertain', output=None):
    return {'id': 'a' * 32, 'batch_id': 'a' * 32, 'kind': kind, 'status': status, 'revision': 1,
        'context': context, 'output': output, 'checks': {}, 'events': [], 'usage': {},
        'reserved_usd': 0, 'actual_usd': None, 'preview_id': None, 'error': None,
        'created_at': '2026-01-01T00:00:00+00:00', 'updated_at': '2026-01-01T00:00:00+00:00'}


def replaced():
    doc = original.document()
    row = doc['rows'][0]
    row.update(ai_job_id='c' * 32, ai_baseline=deepcopy(row['values']))
    doc['automation'] = {'version': 1, 'job_ids': ['c' * 32], 'paused': False,
        'pause_reason': None, 'auto_publish_requested': True}
    draft = {'id': 'd' * 32, 'shop': 'biketrek', 'supplier': 'scope-fixture', 'document': doc}
    source = product_import.to_source(row)
    raw = job({'source_kind': 'import_draft', 'staging_id': draft['id'], 'staging_automation_version': 1,
        'shop': draft['shop'], 'supplier': draft['supplier'], 'code': row['group_key'],
        'product_ids': [row['id']], 'facts': ai_content.facts([source]),
        'source_digest': ai_content.source_digest([source]), 'options': doc['options']})
    return raw, draft


def legacy():
    snapshot = {'product_id': 9001, 'code': 'SYN-SHOP-PARENT', 'ean': None, 'brand': 'Synthetic',
        'descriptions': {'title': 'Synthetic old source'}, 'parameters': [],
        'variants': [{'code': 'SYN-UNRELATED', 'variant_id': 9002, 'ean': None}]}
    return job({'source_kind': 'shop', 'update_only': True, 'shop': 'biketrek', 'feed_key': 'shop',
        'code': snapshot['code'], 'product_ids': [], 'source_snapshot': snapshot,
        'source_digest': ai_content.digest(snapshot), 'archived': True,
        'facts': [{'id': 9001, 'code': snapshot['code'], 'eans': [], 'variant_identity': snapshot['variants']}]},
        status='estimate')


def lookup(status='lookup_failed'):
    output = SlogerObservation(sku='SYN-OLD', eans=[fixture.EAN], source_url='https://www.sloger.sk/item',
        observed_at='2026-01-01T00:00:00+00:00').model_dump(mode='json') if status == 'lookup_completed' else None
    return job({'supplier': 'sloger', 'sku': 'SYN-OLD', 'model': 'gpt-6-luna', 'estimate_usd': '0.01'},
        kind='supplier_lookup', status=status, output=output)


def inspect(raw, draft=None, *, selected=None):
    selected = selected or scope.Scope({1, 2}, set(), {'syn-keep', 'syn-old'}, {fixture.EAN}, set(),
                                     set(), set(), set(), set())
    proof = scope.Proof(selected)
    ai_scope.inspect(proof, raw, {draft['id']: draft} if draft else {}, {})
    return proof


class AiLineageScopeTests(unittest.IsolatedAsyncioTestCase):
    def test_replacement_requires_current_bindings_and_checks_whole_draft(self):
        raw, draft = replaced()
        for status in ('uncertain', 'cancelled', 'review', 'completed'):
            raw['status'] = status
            proof = inspect(raw, draft)
            self.assertEqual(proof.result()['classification'], 'context_only')
            self.assertEqual(proof.linked_drafts, {draft['id']})
            self.assertGreaterEqual(len(proof.queries), 3)
        draft['document']['rows'].append(original.document(source_id=701, leaf='SYN-OLD', variant=False)['rows'][0])
        proof = inspect(raw, draft)
        self.assertEqual(proof.result()['classification'], 'reference')
        self.assertTrue(any(hit['value'] == 'SYN-OLD' for hit in proof.matches))

    def test_absent_partial_or_ambiguous_replacement_never_clears(self):
        mutations = [
            lambda r, d: d['document']['rows'][0].pop('ai_job_id'),
            lambda r, d: d['document']['rows'].append({**original.document(source_id=701)['rows'][0],
                                                     'ai_job_id': r['id']}),
            lambda r, d: d['document']['automation']['job_ids'].append(r['id']),
            lambda r, d: d['document']['automation'].update(job_ids=[]),
            lambda r, d: d['document']['automation'].update(version=True),
            lambda r, d: d['document']['automation'].update(future_writer={'product_id': 2}),
            lambda r, d: d['document'].pop('automation'),
            lambda r, d: r['context'].update(product_ids=[701]),
            lambda r, d: r['context'].update(staging_automation_version=None),
            lambda r, d: r.update(preview_id='e' * 32),
            lambda r, d: r['context'].update(staging_auto_publication={}),
            lambda r, d: r['context'].update(update_preview=None),
            lambda r, d: r['context'].update(update_only=False),
            lambda r, d: r.update(future_native_intent={'product_id': 2}),
            lambda r, d: d['document']['rows'][0]['ai_baseline'].update(code='CHANGED'),
            lambda r, d: d.update(supplier='different-supplier'),
        ]
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=index):
                raw, draft = replaced(); mutation(raw, draft)
                with self.assertRaises(scope.Unverified):
                    inspect(raw, draft)

    def test_replaced_job_output_and_historical_publications_stay_in_scope(self):
        raw, draft = replaced()
        raw['output'] = content(parameters=[{'name': 'Width', 'values': ['1 mm'], 'product_id': 999}]).model_dump()
        with self.assertRaisesRegex(scope.Unverified, 'ai_output_source_id_outside_selection'):
            inspect(raw, draft)
        raw['output'] = None
        draft['document']['previous_publications'] = [{'previews': {'xtrek': 'e' * 32}}]
        self.assertIn(('xtrek', 'e' * 32), inspect(raw, draft).publications)

    async def test_replaced_job_cannot_advance_or_apply_through_actual_current_writers(self):
        raw, frozen = replaced()
        current = SimpleNamespace(**frozen)
        saved = SimpleNamespace(**raw)
        with patch.object(product_import, 'get_draft', AsyncMock(return_value=current)), \
                patch.object(product_import, 'apply_ai', AsyncMock(side_effect=AssertionError('No draft write'))), \
                patch.object(product_import, 'save', AsyncMock(side_effect=AssertionError('No card write'))), \
                patch.object(product_import, 'preview', AsyncMock(side_effect=AssertionError('No native preview'))):
            saved.context['approval'] = 'policy'
            await product_import.advance_ai(None, saved)
        self.assertEqual(saved.status, 'ready')

    def test_legacy_estimate_preserves_parent_and_variant_scope_even_when_archived(self):
        raw = legacy()
        self.assertEqual(inspect(raw).result()['classification'], 'context_only')
        selected = scope.Scope(set(), set(), set(), set(), set(), set(), set(),
            {('biketrek', 9001, 9002)}, {('biketrek', 9001)})
        proof = inspect(raw, selected=selected)
        self.assertEqual(proof.result()['classification'], 'reference')
        self.assertEqual({hit['namespace'] for hit in proof.matches}, {'native_parent_write_id', 'native_variant'})

    def test_legacy_incomplete_snapshot_cannot_hide_an_update_or_new_executor(self):
        mutations = [
            lambda r: r.update(status='ready'), lambda r: r.update(output=content().model_dump()),
            lambda r: r.update(preview_id='e' * 32), lambda r: r.update(checks={'errors': []}),
            lambda r: r['context'].update(source_parameters_loaded=False),
            lambda r: r['context'].update(update_preview={'state': 'uncertain'}),
            lambda r: r['context'].update(update_preview=None),
            lambda r: r['context'].update(update_result={}),
            lambda r: r['context'].update(approval='human'),
            lambda r: r['context'].update(archived=1),
            lambda r: r['context'].update(source_digest='0' * 64),
            lambda r: r['context']['facts'][0].update(id=9003),
            lambda r: r['context']['facts'][0].update(variant_identity=[]),
            lambda r: r['context']['source_snapshot'].update(native_intent={'product_id': 2}),
        ]
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=index):
                raw = legacy(); mutation(raw)
                with self.assertRaises(scope.Unverified):
                    inspect(raw)

    async def test_actual_legacy_start_and_update_source_guards_remain_closed(self):
        raw = legacy()
        with self.assertRaisesRegex(CatalogError, 'older preparation'):
            await ai_content.start(None, SimpleNamespace(**raw))
        with self.assertRaisesRegex(CatalogError, 'older preparation'):
            ai_content_existing.assert_source({}, raw['context'])

    def test_historical_supplier_schema_is_context_regardless_of_matching_sku_or_ean(self):
        for status in ('lookup_running', 'lookup_failed', 'lookup_uncertain', 'lookup_completed'):
            raw = lookup(status)
            proof = inspect(raw)
            self.assertEqual(proof.result()['classification'], 'context_only')
            self.assertFalse(proof.queries or proof.publications or proof.matches)
            if raw['output'] is not None:
                with self.assertRaises(ValidationError):
                    Content.model_validate(raw['output'])

    def test_supplier_lookup_rejects_all_product_intents_even_null(self):
        for key, value in {'shop': 'biketrek', 'code': 'SYN-OLD', 'product_ids': [], 'facts': [],
            'staging_id': None, 'staging_auto_publication': {}, 'update_preview': None,
            'update_only': False, 'retry_import': False, 'future_executor': {}}.items():
            with self.subTest(key=key):
                raw = lookup(); raw['context'][key] = value
                with self.assertRaises(scope.Unverified):
                    inspect(raw)
        for key, value in [('status', 'ready'), ('preview_id', ''), ('preview_id', 'e' * 32),
            ('checks', {'errors': []}), ('output', content().model_dump()), ('future_native_intent', {})]:
            with self.subTest(key=key):
                raw = lookup(); raw[key] = value
                with self.assertRaises(scope.Unverified):
                    inspect(raw)

    def test_supplier_unknown_nested_shapes_and_changed_observation_stay_blocked(self):
        mutations = [
            lambda r: r['context'].update(supplier='different-supplier'),
            lambda r: r['context'].update(estimate_usd='NaN'),
            lambda r: r['context'].update(supplier_diagnostic={'stage': 'login_submit', 'reason': 'http_status', 'native_id': 2}),
            lambda r: r['usage'].update(native_product_id=2),
            lambda r: r['events'].append({'at': '2026-01-01', 'status': 'lookup_failed', 'note': '', 'product_id': 2}),
            lambda r: r['output'].update(sku='OTHER'),
            lambda r: r['output'].update(product_id=2),
            lambda r: r['output'].update(availability_tiers=[{'label': 'Here', 'product_id': 2}]),
        ]
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=index):
                raw = lookup('lookup_completed'); mutation(raw)
                with self.assertRaises(scope.Unverified):
                    inspect(raw)


@unittest.skipUnless(fixture.TEST_URL, 'Dedicated localhost *_catalog_test database required')
class AiLineageDatabaseTests(unittest.IsolatedAsyncioTestCase):
    transaction = fixture.ProductConsolidationDatabaseTests.transaction
    leaf = fixture.ProductConsolidationDatabaseTests.leaf
    discover = fixture.ProductConsolidationDatabaseTests.discover
    verify = fixture.ProductConsolidationDatabaseTests.verify
    absence = fixture.ProductConsolidationDatabaseTests.absence
    code_absence = fixture.ProductConsolidationDatabaseTests.code_absence
    request = fixture.ProductConsolidationDatabaseTests.request
    preview = fixture.ProductConsolidationDatabaseTests.preview
    apply = fixture.ProductConsolidationDatabaseTests.apply
    stock = fixture.ProductConsolidationDatabaseTests.stock
    asyncSetUp = original.ProductDependencyScopeDatabaseTests.asyncSetUp
    asyncTearDown = original.ProductDependencyScopeDatabaseTests.asyncTearDown

    async def add_replaced(self):
        raw, draft = replaced()
        async with self.transaction() as db:
            db.add(ProductImportDraft(id=draft['id'], request_hash='d' * 64, revision=1,
                supplier=draft['supplier'], shop=draft['shop'], status='saved', document=draft['document']))
            db.add(AiBatch(id=raw['batch_id'], request_hash='e' * 64)); await db.flush()
            db.add(AiJob(id=raw['id'], batch_id=raw['batch_id'], kind=raw['kind'], status=raw['status'],
                revision=1, context=raw['context']))
        return raw, draft

    async def test_replaced_lineage_retains_complete_proof_and_preserves_stock_on_apply(self):
        await self.add_replaced()
        before = await self.stock()
        preview = await self.preview()
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        typed = preview['dependencies']['facts']['typed_catalog_scope']
        self.assertTrue(typed['complete'])
        self.assertEqual(typed['linked_manifest']['edges'], 1)
        self.assertEqual((await self.apply(preview))['status'], 'completed')
        self.assertEqual(before, await self.stock())

    async def test_rebinding_after_preview_rejects_apply_and_preserves_history(self):
        raw, draft = await self.add_replaced()
        preview = await self.preview()
        before = await self.stock()
        async with self.transaction() as db:
            await db.execute(text("""UPDATE product_import_drafts
                SET document=jsonb_set(document,'{rows,0,ai_job_id}',to_jsonb(CAST(:job AS text)))
                WHERE id=:draft"""), {'job': raw['id'], 'draft': draft['id']})
        with self.assertRaisesRegex(links.LinkError, 'consolidation_dependencies_changed') as caught:
            await self.apply(preview)
        self.assertTrue(caught.exception.known_unapplied)
        self.assertEqual(before, await self.stock())
        async with self.transaction() as db:
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM product_canonical_aliases')), 0)
