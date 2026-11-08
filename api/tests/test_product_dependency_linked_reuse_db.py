"""Complete immutable linked inputs, bounded reuse and unchanged dependency guards."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import text

import test_product_consolidation_db as fixture
import test_product_dependency_paging_db as paging
import test_product_dependency_reuse_db as resolver
import test_product_dependency_scope_db as original
from inventory_hub.product_import_models import ProductImportDraft
from inventory_hub.services import product_dependency_inventory as inventory
from inventory_hub.services import product_dependency_scan as scan
from inventory_hub.services import product_dependency_scope as scope
from inventory_hub.services import product_links as links
from inventory_hub.services.product_dependency_reuse import ResolutionMemo


class LinkedInputMemoTests(unittest.TestCase):
    def test_raw_copies_preserve_dictionary_order_without_sharing_mutations(self):
        memo = ResolutionMemo(1000, sort_keys=False)
        raw = {'zz': {'b': [1], 'a': 2}, 'a': 3}
        facts = {'count': 1, 'bytes': 300, 'largest': 300}
        memo.put(('table', 'condition', 'params'), raw, metadata=facts)
        hit = memo.get(('table', 'condition', 'params'))
        self.assertEqual(list(hit), ['zz', 'a'])
        self.assertEqual(list(hit['zz']), ['b', 'a'])
        hit['zz']['b'].append(4)
        raw['zz']['b'].append(5)
        self.assertEqual(memo.get(('table', 'condition', 'params'))['zz']['b'], [1])
        facts['count'] = 2
        observed = memo.metadata(('table', 'condition', 'params'))
        observed['bytes'] = 1
        self.assertEqual(memo.metadata(('table', 'condition', 'params')),
                         {'count': 1, 'bytes': 300, 'largest': 300})

    def test_raw_inputs_and_both_caches_share_one_payload_allowance(self):
        identity, linked = ResolutionMemo(4000), ResolutionMemo(16000, sort_keys=False)
        budget = inventory.Budget(16000, 64000, 256000, None, reuse=identity, linked_reuse=linked)
        for number in range(100):
            budget.retain((number * 997) % 64001)
            for memo in (identity, linked):
                memo.put(('synthetic', str(number)), {'value': 'x' * (number * 83 % 17000)})
                budget.account_reuse()
                self.assertLessEqual(budget.retained + identity.bytes + linked.bytes, budget.max_payload)
                self.assertLessEqual(memo.bytes, memo.limit)
        self.assertGreater(identity.evictions + linked.evictions, 0)
        budget.retain(64000)
        self.assertEqual(identity.bytes + linked.bytes, 0)
        self.assertLessEqual(budget.peak_payload, budget.max_payload)


@unittest.skipUnless(fixture.TEST_URL, 'Dedicated localhost *_catalog_test database required')
class LinkedInputReuseDatabaseTests(unittest.IsolatedAsyncioTestCase):
    transaction = paging.DependencyPagingDatabaseTests.transaction
    leaf = paging.DependencyPagingDatabaseTests.leaf
    discover = paging.DependencyPagingDatabaseTests.discover
    verify = paging.DependencyPagingDatabaseTests.verify
    absence = paging.DependencyPagingDatabaseTests.absence
    code_absence = paging.DependencyPagingDatabaseTests.code_absence
    request = paging.DependencyPagingDatabaseTests.request
    current = paging.DependencyPagingDatabaseTests.current
    preview = paging.DependencyPagingDatabaseTests.preview
    apply = paging.DependencyPagingDatabaseTests.apply
    stock = fixture.ProductConsolidationDatabaseTests.stock
    atomer = paging.DependencyPagingDatabaseTests.atomer
    families = paging.DependencyPagingDatabaseTests.families
    add_draft = original.ProductDependencyScopeDatabaseTests.add_draft
    repeated = resolver.DependencyReuseDatabaseTests.repeated
    asyncSetUp = paging.DependencyPagingDatabaseTests.asyncSetUp
    asyncTearDown = paging.DependencyPagingDatabaseTests.asyncTearDown

    @staticmethod
    def logical(report):
        value = deepcopy(report)
        value.pop('work')
        return value

    async def test_complete_proofs_and_each_logical_edge_match_uncached_and_paged_reads(self):
        await self.repeated()
        with patch.object(scan, 'MAX_LINKED_REUSE_BYTES', 0):
            uncached = await self.current()
        reports = []
        for page_rows in (1, 7, 64):
            with patch.object(scan, 'PAGE_ROWS', page_rows):
                report = await self.current()
            self.assertEqual(self.logical(uncached), self.logical(report))
            self.assertTrue(report['complete'])
            self.assertFalse(report['blockers'])
            self.assertEqual(report['linked_manifest']['edges'], 5)
            self.assertEqual(uncached['work']['charged_bytes'] - report['work']['charged_bytes'],
                             report['work']['reused_linked_bytes'])
            self.assertEqual(uncached['work']['charged_rows'] - report['work']['charged_rows'],
                             report['work']['reused_linked_rows'])
            self.assertGreater(report['work']['linked_input_reuse']['hits'], 0)
            reports.append(report)
        self.assertEqual(reports[0], reports[1])
        self.assertEqual(reports[1], reports[2])

    async def test_full_operation_evicts_caches_without_changing_eligibility_or_hashes(self):
        await self.repeated()
        with patch.object(scan, 'MAX_LINKED_REUSE_BYTES', 0), patch.object(scan, 'MAX_RESOLUTION_REUSE_BYTES', 0):
            uncached = await self.current()
        with patch.object(scan, 'MAX_TOTAL_BYTES', uncached['work']['peak_retained_payload_bytes']):
            constrained = await self.current()
        self.assertEqual(self.logical(uncached), self.logical(constrained))
        caches = [constrained['work'][name] for name in ('identity_reuse', 'linked_input_reuse')]
        self.assertGreater(sum(memo['evictions'] + memo['uncached_oversize_results'] for memo in caches), 0)
        self.assertLessEqual(constrained['work']['peak_retained_payload_bytes'],
                             constrained['work']['retained_payload_limit_bytes'])

    async def test_query_scope_absences_and_guard_checks_survive_cache_hits(self):
        identifier, _ = await self.add_draft()
        memo = ResolutionMemo(16000000, sort_keys=False)
        budget = inventory.Budget(16000000, 64000000, 256000000, None, linked_reuse=memo)
        async with self.transaction() as db:
            await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'))
            async def read(condition='t.id=:id', params=None, role='drafts', row_limit=20000):
                closure = inventory.Closure(db, budget=budget, page_rows=1, page_bytes=4000000,
                    retained=0, max_linked_rows=row_limit)
                rows = await closure.rows(role, 'product_import_drafts', condition, params or {'id': identifier})
                return rows, closure.result()
            first, manifest = await read()
            original_row = deepcopy(first[0])
            first[0]['document']['document']['rows'][0]['source']['code'] = 'MUTATED-COPY'
            second, same_manifest = await read()
            self.assertEqual(second, [original_row])
            self.assertEqual(manifest, same_manifest)
            other_role, other_manifest = await read(role='publication_drafts')
            self.assertEqual(other_role, second)
            self.assertNotEqual(other_manifest['hash'], manifest['hash'])
            self.assertEqual((await read("t.id=:id AND t.status='missing'"))[0], [])
            missing = {'id': uuid4().hex}
            self.assertEqual((await read(params=missing))[0], [])
            self.assertEqual((await read(params=missing))[0], [])
            self.assertEqual((await read())[0], [original_row])
            with self.assertRaisesRegex(scope.Unverified, 'linked_identity_scope_limit'):
                await read(row_limit=0)
            budget.max_row = original_row['bytes'] - 1
            with self.assertRaisesRegex(scope.Unverified, 'database_document_limit'):
                await read()
        self.assertGreater(memo.hits, 0)

    async def test_preview_apply_reloads_changed_raw_draft_and_preserves_stock(self):
        draft_id, document = await self.repeated()
        before = await self.stock()
        preview = await self.preview()
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        async with self.transaction() as db:
            draft = await db.get(ProductImportDraft, draft_id)
            changed = deepcopy(document)
            changed['shops'][0]['name'] += '!'
            draft.document = changed
        with self.assertRaises(links.LinkError) as caught:
            await self.apply(preview)
        self.assertEqual(caught.exception.code, 'consolidation_dependencies_changed')
        self.assertTrue(caught.exception.known_unapplied)
        self.assertEqual(before, await self.stock())

    async def test_body_reservation_can_evict_metadata_hit_and_reload_complete_selection(self):
        identifier, _ = await self.add_draft()
        memo = ResolutionMemo(16000000, sort_keys=False)
        budget = inventory.Budget(16000000, 64000000, 256000000, None, linked_reuse=memo)
        async with self.transaction() as db:
            await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'))
            async def read(retained):
                closure = inventory.Closure(db, budget=budget, page_rows=1, page_bytes=4000000,
                    retained=retained, max_linked_rows=20000)
                rows = await closure.rows('drafts', 'product_import_drafts', 't.id=:id', {'id': identifier})
                return rows, closure.result()
            rows, manifest = await read(0)
            key = ('product_import_drafts', 't.id=:id', scope.digest({'id': identifier}))
            self.assertIsNotNone(memo.metadata(key))
            charged = budget.bytes
            retained = budget.max_payload - max(rows[0]['bytes'], memo.bytes)
            reloaded, repeated_manifest = await read(retained)
            self.assertEqual(reloaded, rows)
            self.assertEqual(repeated_manifest, manifest)
            self.assertEqual(budget.bytes - charged, rows[0]['bytes'])
            self.assertGreater(memo.evictions, 0)
            self.assertLessEqual(budget.peak_payload, budget.max_payload)

    async def test_repeated_large_campaign_does_not_hide_tail_or_raise_work_limit(self):
        campaign = await self.families(300, 1, tail_reference=True)
        async with self.transaction() as db:
            await db.execute(text("""UPDATE atomer_catalog_campaigns
                SET selection=jsonb_build_object('synthetic_context',repeat('x',1000000)) WHERE id=:id"""),
                {'id': campaign})
        before = await self.stock()
        with patch.object(scan, 'MAX_LINKED_REUSE_BYTES', 0):
            exhausted = await self.current()
        self.assertFalse(exhausted['complete'])
        self.assertIn('database_total_document_limit', {row.get('reason') for row in exhausted['blockers']})
        report = await self.current()
        self.assertTrue(report['complete'], report['blockers'])
        self.assertEqual(report['record_count'], 301)
        self.assertEqual(report['classification_counts']['atomer_catalog_jobs']['reference'], 1)
        self.assertEqual(report['work']['limit_bytes'], 256000000)
        self.assertLess(report['work']['charged_bytes'], 10000000)
        self.assertGreater(report['work']['reused_linked_bytes'], 256000000)
        self.assertTrue(any(row['classification'] == 'reference' for row in report['records']))
        self.assertEqual(before, await self.stock())
