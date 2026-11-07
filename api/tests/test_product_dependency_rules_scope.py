"""Synthetic rules-only scope, actual rule writers and mixed-intent rejection."""
from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from pydantic import SecretStr, ValidationError
from sqlalchemy import text

import test_product_consolidation_db as fixture
from test_product_dependency_scope_db import document
from test_ai_content import content
from inventory_hub.ai_content_models import AiJob, AiRuleState, AiRuleVersion
from inventory_hub.ai_content_types import (
    CategoryProfile, Content, ContentReview, ProposedInstructions, Rule, RuleBook, RuleProposal,
)
from inventory_hub.db_models import Supplier
from inventory_hub.product_import_models import ProductImportDraft
from inventory_hub.product_import_types import DraftRevision
from inventory_hub.services import ai_content, ai_content_worker as worker, product_import
from inventory_hub.services import product_dependency_ai_scope as ai_scope, product_dependency_scope as scope
from inventory_hub.services import product_links as links
from inventory_hub.services.ai_content_rules import DEFAULT_POLICY
from inventory_hub.settings import settings


def proposal_output():
    return ProposedInstructions(instructions='Describe only supported facts.', reason='Synthetic editorial revision.',
        questions=[], parameters=None).model_dump()


def rule_job(*, category=False, status='estimate'):
    current = (CategoryProfile(id='synthetic', name='Synthetic category') if category else
        Rule(id='synthetic', name='Synthetic rule', scope={'product': 'SYN-OLD'}, instructions='SYN-KEEP is prose.'))
    ctx = {'rules_version': 1, 'current': current.model_dump(), 'proposal_request': 'Clarify this rule.',
        'category': category, 'use_ai': True, 'model': 'gpt-6-luna', 'name': current.name,
        'resolved': {'policy': dict(DEFAULT_POLICY), 'origins': {}}, 'estimate_usd': '0.01',
        **ai_content.provider.generation_settings('gpt-6-luna')}
    return {'id': 'a' * 32, 'batch_id': 'b' * 32, 'kind': 'rules', 'status': status, 'revision': 1,
        'context': ctx, 'output': proposal_output() if status in ('review', 'completed') else None,
        'checks': {}, 'events': [], 'usage': {}, 'reserved_usd': 0, 'actual_usd': None,
        'preview_id': None, 'error': None, 'created_at': '2026-01-01T00:00:00+00:00',
        'updated_at': '2026-01-01T00:00:00+00:00'}


def inspect(raw):
    selected = scope.Scope({1, 2}, set(), {'syn-keep', 'syn-old'}, set(), set(), set(), set(), set(), set())
    proof = scope.Proof(selected)
    ai_scope.inspect(proof, raw, {}, {})
    return proof.result()


class RuleScopeTests(unittest.TestCase):
    def test_known_proposals_are_context_with_no_product_queries_or_publications(self):
        for category in (False, True):
            for status in ('estimate', 'queued', 'generating', 'review', 'completed', 'failed', 'uncertain', 'cancelled'):
                with self.subTest(category=category, status=status):
                    raw = rule_job(category=category, status=status)
                    raw['context']['archived'] = True
                    result = inspect(raw)
                    self.assertEqual(result['classification'], 'context_only')
                    self.assertEqual(result['matched_paths'], [])
                    self.assertEqual(result['linked_drafts'], [])
                    self.assertEqual(result['publication_journals'], [])

    def test_product_and_publication_context_is_rejected_even_when_empty(self):
        for key, value in {'shop': 'biketrek', 'product_ids': [], 'facts': [], 'source_kind': None,
            'staging_id': None, 'staging_auto_publication': {}, 'update_preview': None, 'update_only': False,
            'approval': None, 'target': '', 'code': 'SYN-OLD', 'retry_import': False,
            'classification_catalog': {}, 'future_executor': {'product_id': 2}}.items():
            with self.subTest(key=key):
                raw = rule_job(); raw['context'][key] = value
                with self.assertRaises(scope.Unverified):
                    inspect(raw)

    def test_persisted_publication_product_checks_and_import_stages_are_rejected(self):
        for key, value in [('preview_id', ''), ('preview_id', 'c' * 32), ('checks', {'errors': []}),
            ('status', 'ready'), ('status', 'preparing_import'), ('status', 'import_queued'),
            ('status', 'importing'), ('status', 'exists'), ('status', 'import_failed')]:
            with self.subTest(key=key, value=value):
                raw = rule_job(); raw[key] = value
                with self.assertRaises(scope.Unverified):
                    inspect(raw)

    def test_rule_output_cannot_be_used_as_product_content(self):
        raw = rule_job(status='review')
        with self.assertRaises(ValidationError):
            Content.model_validate(raw['output'])
        for output in (content().model_dump(), {**raw['output'], 'title': 'SYN-OLD'},
            {**raw['output'], 'parameters': [{'name': 'Size', 'product_id': 2}]}, {}):
            with self.subTest(output_keys=sorted(output)):
                changed = deepcopy(raw); changed['output'] = output
                with self.assertRaises(scope.Unverified):
                    inspect(changed)

    def test_nested_rule_scope_policy_and_metadata_unknowns_stay_blocked(self):
        mutations = [
            lambda r: r.update(future_native_intent={'product_id': 2}),
            lambda r: r['context']['current']['scope'].update(product_id=2),
            lambda r: r['context']['current'].update(publication={'product_id': 2}),
            lambda r: r['context']['resolved'].update(category={'product_id': 2}),
            lambda r: r['context']['resolved']['origins'].update(product_id=2),
            lambda r: r['context']['resolved']['policy'].update(confirm_import=False),
            lambda r: r['usage'].update(native_product_id=2),
            lambda r: r['events'].append({'at': '2026-01-01T00:00:00Z', 'status': 'review',
                                        'note': 'Synthetic', 'product_id': 2}),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutations.index(mutation)):
                raw = rule_job(); mutation(raw)
                with self.assertRaises(scope.Unverified):
                    inspect(raw)

    def test_native_reference_snapshot_is_outside_initial_contract(self):
        raw = rule_job(category=True)
        raw['context']['current']['reference_products'] = [{'shop': 'biketrek', 'code': 'SYN-OLD',
            'product_id': 2, 'language': 'sk', 'captured_at': '2026-01-01T00:00:00Z',
            'content': {'title': 'Synthetic reference'}}]
        with self.assertRaisesRegex(scope.Unverified, 'ai_rule_native_reference_unverified'):
            inspect(raw)

    def test_contradictory_scalar_or_output_lifecycle_is_not_normalized(self):
        mutations = [
            lambda r: r['context'].update(category='false'),
            lambda r: r['context'].update(use_ai=False),
            lambda r: r['context'].update(rules_version=True),
            lambda r: r['context'].update(name='Another rule'),
            lambda r: r['context'].update(estimate_usd='NaN'),
            lambda r: r['context'].update(archived=1),
            lambda r: r['context'].update(prompt_version=True),
            lambda r: r['context']['resolved']['policy'].update(confirm_import=1),
            lambda r: r.update(status='review'),
            lambda r: r.update(output=proposal_output()),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutations.index(mutation)):
                raw = rule_job(); mutation(raw)
                with self.assertRaises(scope.Unverified):
                    inspect(raw)

    def test_unknown_ai_kinds_do_not_gain_rules_exception(self):
        for kind in ('supplier_lookup', 'future_kind', 'product', None):
            with self.subTest(kind=kind):
                raw = rule_job(); raw['kind'] = kind
                with self.assertRaises(scope.Unverified):
                    inspect(raw)


@unittest.skipUnless(fixture.TEST_URL, 'Dedicated localhost *_catalog_test database required')
class RuleScopeDatabaseTests(unittest.IsolatedAsyncioTestCase):
    transaction = fixture.ProductConsolidationDatabaseTests.transaction
    leaf = fixture.ProductConsolidationDatabaseTests.leaf
    discover = fixture.ProductConsolidationDatabaseTests.discover
    verify = fixture.ProductConsolidationDatabaseTests.verify
    absence = fixture.ProductConsolidationDatabaseTests.absence
    code_absence = fixture.ProductConsolidationDatabaseTests.code_absence
    preview = fixture.ProductConsolidationDatabaseTests.preview
    apply = fixture.ProductConsolidationDatabaseTests.apply
    stock = fixture.ProductConsolidationDatabaseTests.stock
    request = fixture.ProductConsolidationDatabaseTests.request

    async def asyncSetUp(self):
        await fixture.ProductConsolidationDatabaseTests.asyncSetUp(self)
        book = RuleBook(rules=[Rule(id='synthetic', name='Synthetic rule', scope={'product': 'SYN-OLD'})],
            categories=[CategoryProfile(id='synthetic', name='Synthetic category')])
        async with self.transaction() as db:
            version = AiRuleVersion(book=book.model_dump(), note='Synthetic rules', origin='test')
            db.add(version); await db.flush()
            db.add(AiRuleState(id=1, published_id=version.id))
            db.add(Supplier(code='scope-fixture', name='Synthetic scope supplier'))
            self.published = version.id
        self.local_patches = [
            patch.object(ai_content, 'effective_model', AsyncMock(return_value='gpt-6-luna')),
            patch.object(settings, 'AI_CONTENT_ENABLED', True),
            patch.object(settings, 'OPENAI_API_KEY', SecretStr('synthetic-no-network')),
            patch.object(settings, 'AI_CONTENT_JOB_USD', 10),
            patch.object(settings, 'AI_CONTENT_MONTHLY_USD', 100),
            patch.object(worker, 'get_session_context', self.transaction),
            patch.object(ai_content.catalog_import.UpgatesClient, 'from_shop', side_effect=AssertionError('No native API allowed')),
        ]
        for patcher in self.local_patches:
            patcher.start()

    async def asyncTearDown(self):
        for patcher in reversed(self.local_patches):
            patcher.stop()
        await fixture.ProductConsolidationDatabaseTests.asyncTearDown(self)

    async def create(self, *, category=False):
        async with self.transaction() as db:
            result = await ai_content.propose_rule(db, RuleProposal(request_id=uuid4(), rule_id='synthetic',
                category=category, request='Clarify this synthetic rule.'))
            return result['id']

    async def raw(self, identifier):
        async with self.transaction() as db:
            return await db.scalar(text('SELECT to_jsonb(t) FROM ai_content_jobs t WHERE id=:id'), {'id': identifier})

    async def generate(self, identifier):
        async with self.transaction() as db:
            await ai_content.start(db, await ai_content.get_job(db, identifier, lock=True))
        response = {'id': 'resp_synthetic', 'status': 'completed', 'usage': {'input_tokens': 100, 'output_tokens': 50},
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(proposal_output())}]}]}
        with patch.object(worker.provider, 'generate', AsyncMock(return_value=response)) as generate:
            await worker.generation(identifier)
            self.assertEqual(generate.await_args.args[1], 'rules')

    def record(self, preview, identifier):
        return next(row for row in preview['dependencies']['facts']['typed_catalog_scope']['records']
            if row['table'] == 'ai_content_jobs' and row['row_identity']['id'] == identifier)

    async def test_actual_propose_worker_and_accept_save_only_an_unpublished_version(self):
        before = await self.stock()
        for category in (False, True):
            with self.subTest(category=category):
                identifier = await self.create(category=category)
                self.assertEqual(inspect(await self.raw(identifier))['classification'], 'context_only')
                await self.generate(identifier)
                self.assertEqual(inspect(await self.raw(identifier))['classification'], 'context_only')
                async with self.transaction() as db:
                    job = await ai_content.get_job(db, identifier, lock=True)
                    accepted = await ai_content.accept_proposal(db, job, job.revision)
                    self.assertFalse(accepted['published'])
                    self.assertNotEqual(accepted['version_id'], self.published)
                self.assertEqual(inspect(await self.raw(identifier))['classification'], 'context_only')
        async with self.transaction() as db:
            self.assertEqual((await db.get(AiRuleState, 1)).published_id, self.published)
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM products')), 2)
        self.assertEqual(await self.stock(), before)
        preview = await self.preview()
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        self.assertEqual(self.record(preview, identifier)['classification'], 'context_only')
        self.assertTrue(self.record(preview, identifier)['row_hash'])
        self.assertEqual((await self.apply(preview))['status'], 'completed')
        self.assertEqual(await self.stock(), before)

    async def test_rule_row_still_participates_in_preview_hash_and_apply_revalidation(self):
        identifier = await self.create()
        before = await self.stock()
        preview = await self.preview()
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        async with self.transaction() as db:
            job = await db.get(AiJob, identifier)
            job.context = {**job.context, 'proposal_request': 'Another legitimate synthetic request.'}
        with self.assertRaisesRegex(links.LinkError, 'consolidation_dependencies_changed'):
            await self.apply(preview)
        self.assertEqual(await self.stock(), before)
        async with self.transaction() as db:
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM product_canonical_aliases')), 0)

    async def test_real_rule_job_with_mixed_native_intent_remains_unknown(self):
        identifier = await self.create()
        async with self.transaction() as db:
            job = await db.get(AiJob, identifier)
            job.context = {**job.context, 'update_preview': {'state': 'uncertain',
                'identity': {'product_id': 9002, 'code': 'SYN-OLD', 'variants': []}}}
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        self.assertEqual(self.record(preview, identifier)['classification'], 'unknown')

    async def test_actual_product_readers_reject_rules_output_before_mutation(self):
        identifier = await self.create(); await self.generate(identifier)
        raw_before = await self.raw(identifier)
        # Generic content review lacks a kind guard, but the known rules context
        # has no facts; validation raises before writing Content into this job.
        with self.assertRaises(KeyError):
            async with self.transaction() as db:
                job = await ai_content.get_job(db, identifier, lock=True)
                await ai_content.review(db, job, ContentReview(expected_revision=job.revision, content=content()))
        self.assertEqual(await self.raw(identifier), raw_before)
        doc = document(leaf='SYN-OLD', variant=False)
        doc['rows'][0].update(ai_job_id=identifier, hub_product_id=self.retired)
        doc['rows'][0]['values']['ai_enabled'] = True
        draft_id = uuid4().hex
        async with self.transaction() as db:
            db.add(ProductImportDraft(id=draft_id, request_hash='f' * 64, revision=1,
                supplier='scope-fixture', shop='biketrek', status='draft', document=doc))
        with self.assertRaises(ValidationError):
            async with self.transaction() as db:
                await product_import.apply_ai(db, await db.get(ProductImportDraft, draft_id), DraftRevision(expected_revision=1))
        self.assertEqual(await self.raw(identifier), raw_before)
        async with self.transaction() as db:
            self.assertEqual((await db.get(ProductImportDraft, draft_id)).document, doc)
        # Accepting the rule's own scope never exempts an independently linked
        # draft from the complete inventory or its physical-card dependency.
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        self.assertEqual(self.record(preview, identifier)['classification'], 'context_only')
        draft_record = next(row for row in preview['dependencies']['facts']['typed_catalog_scope']['records']
            if row['row_identity'].get('id') == draft_id)
        self.assertEqual(draft_record['classification'], 'reference')

    async def test_uncertain_rule_provider_state_does_not_claim_uncertain_product_safety(self):
        identifier = await self.create()
        async with self.transaction() as db:
            await ai_content.start(db, await ai_content.get_job(db, identifier, lock=True))
        with patch.object(worker.provider, 'generate', AsyncMock(side_effect=
                ai_content.CatalogError('ai_outcome_unknown', 'Synthetic interrupted provider request', 502))):
            await worker.generation(identifier)
        raw = await self.raw(identifier)
        self.assertEqual(raw['status'], 'uncertain')
        self.assertIsNone(raw['output'])
        self.assertEqual(inspect(raw)['classification'], 'context_only')
        changed = deepcopy(raw); changed['kind'] = 'product'
        with self.assertRaises(scope.Unverified):
            inspect(changed)
