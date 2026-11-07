"""Synthetic complete paging, bounded projections, workload and rollback gates."""
import asyncio
from copy import deepcopy
import json
import resource
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import event, text

import test_product_consolidation_db as fixture
import test_product_dependency_scope_db as original
from inventory_hub.ai_content_models import AiBatch, AiJob
from inventory_hub.product_import_models import ProductImportDraft
from inventory_hub.services import product_consolidation as service
from inventory_hub.services import product_consolidation_dependencies as dependencies
from inventory_hub.services import product_consolidation_diagnostics as diagnostics
from inventory_hub.services import product_dependency_inventory as inventory
from inventory_hub.services import product_dependency_scan as scan
from inventory_hub.services import product_links as links


@unittest.skipUnless(fixture.TEST_URL, 'Dedicated localhost *_catalog_test database required')
class DependencyPagingDatabaseTests(unittest.IsolatedAsyncioTestCase):
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
    atomer = original.ProductDependencyScopeDatabaseTests.atomer
    add_ai = original.ProductDependencyScopeDatabaseTests.add_ai
    add_draft = original.ProductDependencyScopeDatabaseTests.add_draft
    journal = original.ProductDependencyScopeDatabaseTests.journal

    async def asyncSetUp(self):
        await original.ProductDependencyScopeDatabaseTests.asyncSetUp(self)

    async def asyncTearDown(self):
        await original.ProductDependencyScopeDatabaseTests.asyncTearDown(self)

    async def current(self):
        async with self.transaction() as db:
            await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'))
            raw = service._request(self.request())
            local = await service._local(db, raw)
            return (await dependencies.inspect(db, ids=service._ids(raw), keeper_id=self.keeper,
                products=local['products'], identifiers=local['identifiers'], mappings=local['mappings'],
                targets=service._targets(raw), request_id=raw['request_id']))['facts']['typed_catalog_scope']

    async def families(self, count, leaves, *, padding=0, tail_reference=False):
        campaign, _ = await self.atomer()
        async with self.transaction() as db:
            run = await db.scalar(text('SELECT checked_run_id FROM atomer_catalog_campaigns WHERE id=:id'),
                                  {'id': campaign})
            await db.execute(text('''INSERT INTO atomer_progress_items(run_id,code,kind,state,family_key,detail)
                SELECT :run,'SYN-PAGE-' || lpad(n::text,5,'0') || '-' || lpad(k::text,2,'0'),
                    'standalone','needs_both','s:synthetic-page-' || lpad(n::text,5,'0'),
                    jsonb_build_object('code','SYN-PAGE-' || lpad(n::text,5,'0') || '-' || lpad(k::text,2,'0'),
                        'kind','standalone','name',repeat('x',:padding),'hub_id',
                        CASE WHEN n=:count AND k=:leaves AND :tail THEN CAST(:retired AS bigint) ELSE NULL END)
                FROM generate_series(1,:count) n CROSS JOIN generate_series(1,:leaves) k'''),
                {'run': run, 'count': count, 'leaves': leaves, 'padding': padding,
                 'tail': tail_reference, 'retired': self.retired})
            await db.execute(text('''INSERT INTO atomer_catalog_jobs
                (campaign,family_key,priority,leaf_count,stage,guards,receipt)
                SELECT :campaign,family_key,1,count(*),'pending',jsonb_build_object(
                    'group',jsonb_build_array(0,'open','default'),
                    'rows',jsonb_object_agg(code,jsonb_build_array(0,'open','default')),
                    'scope',jsonb_object_agg(code,'"default"'::jsonb),
                    'own','[]'::jsonb,'excluded','{}'::jsonb,'blocked','{}'::jsonb),'{}'::jsonb
                FROM atomer_progress_items WHERE run_id=:run AND family_key LIKE 's:synthetic-page-%'
                GROUP BY family_key'''), {'run': run, 'campaign': campaign})
        return campaign

    async def test_page_size_independent_complete_roots_and_cross_page_ai_closure(self):
        await self.families(9, 3)
        await self.add_ai(second_target=True)
        self.journal()
        reports = []
        for rows in (1, 7, 64):
            with patch.object(scan, 'PAGE_ROWS', rows):
                reports.append(await self.current())
        self.assertTrue(all(row['complete'] for row in reports))
        self.assertEqual(reports[0], reports[1])
        self.assertEqual(reports[1], reports[2])
        self.assertEqual(reports[0]['classification_counts']['ai_content_jobs']['reference'], 1)

    async def test_composite_text_cursor_preserves_database_c_order_and_exhaustion(self):
        campaign, _ = await self.atomer()
        names = ['s:Z', 's:a', 's:A', 's:é', 's:ž', 's:10', 's:2']
        async with self.transaction() as db:
            await db.execute(text('''INSERT INTO atomer_catalog_jobs(campaign,family_key,priority,leaf_count)
                SELECT :c,unnest(CAST(:names AS text[])),1,1'''), {'c': campaign, 'names': names})
        reports = []
        for rows in (1, 7, 64):
            with patch.object(scan, 'PAGE_ROWS', rows):
                reports.append(await self.current())
        self.assertEqual(reports[0], reports[1])
        self.assertEqual(reports[1], reports[2])
        self.assertEqual(reports[0]['tables']['atomer_catalog_jobs']['count'], 8)
        self.assertEqual(reports[0]['classification_counts']['atomer_catalog_jobs']['unknown'], 7)

    async def test_tail_reference_beyond_old_job_and_join_limits_is_counted_and_sampled(self):
        await self.families(1001, 21, tail_reference=True)
        before = await self.stock()
        report = await self.current()
        self.assertTrue(report['complete'], report['blockers'])
        self.assertEqual(report['record_count'], 1002)
        self.assertEqual(report['classification_counts']['atomer_catalog_jobs']['reference'], 1)
        self.assertGreater(report['linked_manifest']['edges'], 21000)
        self.assertFalse(report['record_projection_complete'])
        self.assertEqual(report['reason_counts']['atomer_catalog_jobs']['reference']['reference:hub_product_id'], 1)
        self.assertTrue(any(r['classification'] == 'reference' for r in report['records']))
        self.assertLessEqual(len(inventory.encoded(report['records'])), scan.MAX_SAMPLE_BYTES + 1000)
        self.assertEqual(await self.stock(), before)

    async def test_late_unknown_histogram_covers_every_reason_without_raw_values(self):
        campaign = await self.families(30, 1)
        async with self.transaction() as db:
            await db.execute(text('''UPDATE atomer_catalog_jobs SET receipt='{"PRIVATE-KEY":"PRIVATE-VALUE"}'
                WHERE campaign=:c AND family_key IN ('s:synthetic-page-00029','s:synthetic-page-00030')'''), {'c': campaign})
        report = await self.current()
        self.assertTrue(report['complete'])
        self.assertEqual(report['reason_counts']['atomer_catalog_jobs']['unknown']['unknown_schema_field'], 2)
        self.assertTrue(any(r['classification'] == 'unknown' for r in report['records']))
        self.assertNotIn('PRIVATE-', json.dumps(report))

    async def test_late_recognized_ai_kind_gets_sample_even_with_same_unknown_reason(self):
        batch = uuid4().hex
        async with self.transaction() as db:
            db.add(AiBatch(id=batch, request_hash='e' * 64)); await db.flush()
            for number in range(1, 32):
                db.add(AiJob(id=f'{number:032x}', batch_id=batch,
                    kind='product' if number < 31 else 'supplier_lookup', status='ready', revision=1, context={}))
        report = await self.current()
        self.assertTrue(report['complete'])
        self.assertEqual(report['classification_counts']['ai_content_jobs']['unknown'], 31)
        self.assertEqual(report['reason_counts']['ai_content_jobs']['unknown']['incomplete_object'], 31)
        self.assertEqual({row['lifecycle']['kind'] for row in report['records']}, {'product', 'supplier_lookup'})

    async def test_empty_and_short_selections_do_not_need_an_unbounded_tail_read(self):
        await self.add_draft()
        with patch.object(scan, 'PAGE_ROWS', 1):
            report = await self.current()
        self.assertTrue(report['complete'])
        self.assertEqual(report['tables']['ai_content_jobs']['count'], 0)
        self.assertEqual(report['tables']['product_import_drafts']['count'], 1)

    async def test_row_work_and_materialized_payload_limits_are_independent_holds(self):
        await self.add_ai()
        for name in ('MAX_ROW_BYTES', 'MAX_WORK_BYTES', 'MAX_TOTAL_BYTES'):
            with self.subTest(limit=name), patch.object(scan, name, 1):
                report = await self.current()
                self.assertFalse(report['complete'])
                self.assertIn('consolidation_dependency_scope_unverified', {r['code'] for r in report['blockers']})
        with patch.object(scan, 'MAX_EXTRA_ROWS', 0):
            report = await self.current()
        self.assertFalse(report['complete'])
        self.assertIn('identity_namespace_limit', {r.get('reason') for r in report['blockers']})

    async def test_old_prepared_inventory_version_requires_new_preview_but_completed_replays(self):
        with patch.object(scan.s, 'VERSION', 'typed-catalog-scope-v1'):
            preview = await self.preview()
        with self.assertRaisesRegex(links.LinkError, 'consolidation_dependency_inventory_version_changed'):
            await self.apply(preview)
        with patch.object(scan.s, 'VERSION', 'typed-catalog-scope-v1'):
            self.assertEqual((await self.apply(preview))['status'], 'completed')
        self.assertEqual((await self.apply(preview))['status'], 'completed')

    async def slow_statistics(self, db, *args, **kwargs):
        await db.execute(text('SELECT pg_sleep(5)'))
        return await self.original_statistics(db, *args, **kwargs)

    async def test_timeout_cancels_sql_rolls_back_and_never_persists_partial_preview(self):
        self.original_statistics = inventory.statistics
        before = await self.stock()
        started = time.monotonic()
        with patch.object(scan, 'MAX_SCAN_SECONDS', .1), patch.object(inventory, 'statistics', side_effect=self.slow_statistics):
            with self.assertRaises(scan.InventoryTimeout) as caught:
                await self.preview()
        self.assertTrue(caught.exception.known_unapplied)
        self.assertFalse(caught.exception.report['complete'])
        self.assertLess(time.monotonic() - started, 3)
        async with self.transaction() as db:
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM product_consolidations')), 0)
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM product_canonical_aliases')), 0)
        self.assertEqual(await self.stock(), before)

    async def test_synchronous_elapsed_deadline_uses_same_rollback_without_prepared_row(self):
        original_check = inventory.Budget.check_time
        def expired(budget):
            budget.started -= budget.max_seconds + 1
            original_check(budget)
        with patch.object(inventory.Budget, 'check_time', expired):
            with self.assertRaises(scan.InventoryTimeout) as caught:
                await self.preview()
        self.assertTrue(caught.exception.known_unapplied)
        self.assertFalse(caught.exception.report['complete'])
        async with self.transaction() as db:
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM product_consolidations')), 0)
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM product_canonical_aliases')), 0)

    async def test_timeout_diagnostic_returns_plain_incomplete_report_without_further_sql(self):
        preview = await self.preview()
        self.original_statistics = inventory.statistics
        with patch.object(scan, 'MAX_SCAN_SECONDS', .1), patch.object(inventory, 'statistics', side_effect=self.slow_statistics), \
                patch.object(diagnostics, '_documents', side_effect=AssertionError('No SQL after rollback')):
            async with self.transaction() as db:
                result = await diagnostics.inspect(db, preview['id'])
                self.assertEqual(await db.scalar(text('SELECT 1')), 1)
        self.assertFalse(result['complete'])
        self.assertFalse(result['typed_scope']['inventory_complete'])
        self.assertTrue(result['scope_inventory']['complete'])
        self.assertEqual(result['problems'], ['database_inventory_time_limit'])

    async def test_timeout_apply_uses_existing_expiry_fence_and_preserves_stock(self):
        preview = await self.preview()
        self.original_statistics = inventory.statistics
        before = await self.stock()
        with patch.object(scan, 'MAX_SCAN_SECONDS', .1), patch.object(inventory, 'statistics', side_effect=self.slow_statistics):
            with self.assertRaises(scan.InventoryTimeout) as caught:
                await self.apply(preview)
        self.assertTrue(caught.exception.known_unapplied)
        async with self.transaction() as db:
            self.assertEqual(await db.scalar(text('SELECT status FROM product_consolidations WHERE id=:id'),
                                             {'id': preview['id']}), 'expired')
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM product_canonical_aliases')), 0)
        self.assertEqual(await self.stock(), before)

    async def test_representative_large_inventory_benchmark_is_complete_and_bounded(self):
        await self.families(4425, 8, padding=2200)
        queries = 0
        def query(*args):
            nonlocal queries
            queries += 1
        event.listen(self.engine.sync_engine, 'before_cursor_execute', query)
        started, rss_before = time.monotonic(), resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        try:
            report = await self.current()
        finally:
            event.remove(self.engine.sync_engine, 'before_cursor_execute', query)
        metrics = {'seconds': round(time.monotonic() - started, 3), 'sql_queries': queries,
            'jobs': report['record_count'], 'work': report['work'],
            'process_peak_rss_kib_before': rss_before,
            'process_peak_rss_kib_after': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
        print('PAGING_BENCHMARK ' + json.dumps(metrics, sort_keys=True), flush=True)
        self.assertTrue(report['complete'], report['blockers'])
        self.assertFalse(report['blockers'])
        self.assertGreater(report['work']['charged_bytes'], 83000000)
        self.assertLess(report['work']['charged_bytes'], scan.MAX_WORK_BYTES)
        self.assertLessEqual(report['work']['peak_retained_payload_bytes'], scan.MAX_TOTAL_BYTES)
        self.assertLess(metrics['seconds'], scan.MAX_SCAN_SECONDS)
