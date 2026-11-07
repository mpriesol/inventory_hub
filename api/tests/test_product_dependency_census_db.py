"""Synthetic PostgreSQL census limits, privacy, snapshot and no-write checks."""
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import text

import test_product_consolidation_db as fixture
import test_product_dependency_scope_db as scope_fixture
from inventory_hub.ai_content_models import AiBatch, AiJob
from inventory_hub.services import product_consolidation_dependencies as dependencies
from inventory_hub.services import product_consolidation_diagnostics as diagnostics
from inventory_hub.services import product_dependency_census as census
from inventory_hub.services import product_dependency_scan as scan
from inventory_hub.services import product_links as links


@unittest.skipUnless(fixture.TEST_URL, 'Dedicated localhost *_catalog_test database required')
class DependencyCensusDatabaseTests(unittest.IsolatedAsyncioTestCase):
    transaction = fixture.ProductConsolidationDatabaseTests.transaction
    leaf = fixture.ProductConsolidationDatabaseTests.leaf
    discover = fixture.ProductConsolidationDatabaseTests.discover
    verify = fixture.ProductConsolidationDatabaseTests.verify
    absence = fixture.ProductConsolidationDatabaseTests.absence
    code_absence = fixture.ProductConsolidationDatabaseTests.code_absence
    request = fixture.ProductConsolidationDatabaseTests.request
    preview = fixture.ProductConsolidationDatabaseTests.preview
    stock = fixture.ProductConsolidationDatabaseTests.stock
    atomer = scope_fixture.ProductDependencyScopeDatabaseTests.atomer
    add_draft = scope_fixture.ProductDependencyScopeDatabaseTests.add_draft

    async def asyncSetUp(self):
        await scope_fixture.ProductDependencyScopeDatabaseTests.asyncSetUp(self)

    async def asyncTearDown(self):
        await scope_fixture.ProductDependencyScopeDatabaseTests.asyncTearDown(self)

    async def inventory(self, request_id=None):
        async with self.transaction() as db:
            await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
            return await census.inspect(db, request_id or uuid4(), open_condition=dependencies._open_condition)

    async def report(self, identifier):
        async with self.transaction() as db:
            return await diagnostics.inspect(db, identifier)

    async def raw_hashes(self):
        async with self.transaction() as db:
            return {table: await db.scalar(text(f'''SELECT md5(coalesce(
                string_agg(to_jsonb(t)::text,E'\\n' ORDER BY to_jsonb(t)::text),'')) FROM {table} t'''))
                for table in sorted(scan.TABLES)}

    async def test_all_table_census_survives_first_loader_cap_before_parsing(self):
        campaign, _ = await self.atomer()
        await self.add_draft()
        async with self.transaction() as db:
            await db.execute(text('''INSERT INTO atomer_catalog_jobs(campaign,family_key,priority,leaf_count)
                SELECT :campaign,'s:synthetic-census-' || n,1,1 FROM generate_series(1,1000) n'''),
                {'campaign': campaign})
        with patch.object(scan, 'MAX_WORK_BYTES', 1):
            preview = await self.preview()
        self.assertFalse(preview['eligible'])
        self.assertEqual(preview['dependencies']['facts']['typed_catalog_scope']['records'], [])
        before, stock = await self.raw_hashes(), await self.stock()
        calls = []
        original_census, original_rows = census.inspect, scan._inspect
        async def record_census(*args, **kwargs):
            result = await original_census(*args, **kwargs)
            calls.append('census_complete')
            return result
        async def record_rows(*args, **kwargs):
            calls.append('loader')
            return await original_rows(*args, **kwargs)
        with patch.object(census, 'inspect', side_effect=record_census), \
             patch.object(scan, '_inspect', side_effect=record_rows), \
             patch.object(scan, 'MAX_WORK_BYTES', 1), \
             patch.object(links.source, 'discover', side_effect=AssertionError('No native read')):
            report = await self.report(preview['id'])
        self.assertEqual(calls[0], 'census_complete')
        meta = report['scope_inventory']
        self.assertTrue(meta['complete'])
        self.assertEqual(set(meta['tables']), scan.TABLES)
        self.assertEqual(meta['tables']['atomer_catalog_jobs']['count'], 1001)
        self.assertEqual(meta['tables']['atomer_catalog_jobs']['exceeded_limits'], ['bytes'])
        self.assertIsNone(meta['tables']['atomer_catalog_jobs']['limits']['count'])
        self.assertEqual(meta['tables']['product_import_drafts']['count'], 1)
        self.assertEqual(meta['tables']['product_consolidations']['count'], 0)
        self.assertEqual(meta['atomer_current_families']['count'], 1)
        self.assertEqual(meta['atomer_current_families']['open_jobs_without_current_family'], 1000)
        self.assertEqual(report['current_blockers'], preview['dependencies']['blockers'])
        self.assertEqual(await self.raw_hashes(), before)
        self.assertEqual(await self.stock(), stock)

    async def test_exact_full_row_sizes_and_each_limit_are_separate_metadata(self):
        await self.add_draft()
        async with self.transaction() as db:
            expected = (await db.execute(text('''SELECT count(*) AS count,
                sum(octet_length(to_jsonb(t)::text)) AS bytes,
                max(octet_length(to_jsonb(t)::text)) AS largest FROM product_import_drafts t'''))).mappings().one()
        with patch.object(scan, 'MAX_WORK_BYTES', 1), patch.object(scan, 'MAX_TOTAL_BYTES', 1), \
             patch.object(scan, 'MAX_ROW_BYTES', 1):
            meta = await self.inventory()
        actual = meta['tables']['product_import_drafts']
        self.assertEqual({key: actual[key] for key in expected}, dict(expected))
        self.assertEqual(actual['exceeded_limits'], ['bytes', 'largest'])
        self.assertTrue(meta['known_tables_total_bytes_exceeds_limit'])
        self.assertGreater(meta['known_and_atomer_join_bytes'], meta['combined_bytes_limit'])

    async def test_recognized_lifecycle_buckets_never_echo_unknown_text_or_documents(self):
        batch = uuid4().hex
        async with self.transaction() as db:
            db.add(AiBatch(id=batch, request_hash='b' * 64)); await db.flush()
            for kind, status, ctx in (
                ('product', 'completed', {'prompt': 'PRIVATE-PROMPT'}),
                ('rules', 'review', {'update_preview': {'state': 'sending'}}),
                ('PRIVATE-KIND', 'PRIVATE-STATUS', {'update_preview': {'state': 'PRIVATE-STATE'},
                                                   'PRIVATE-KEY': 'PRIVATE-INVOICE'}),
                ('product', 'completed', {'update_preview': {'state': 'completed'}})):
                db.add(AiJob(id=uuid4().hex, batch_id=batch, kind=kind, status=status, revision=1, context=ctx))
        meta = await self.inventory()
        entry = meta['tables']['ai_content_jobs']
        self.assertEqual(entry['count'], 3)  # Only the settled update-preview is outside current scope.
        shapes = entry['lifecycle_shapes']
        self.assertEqual(shapes['kind'], {'other': 1, 'product': 1, 'rules': 1})
        self.assertEqual(shapes['status'], {'completed': 1, 'other': 1, 'review': 1})
        self.assertEqual(shapes['current_publication_state'], {'missing_or_sql_null': 1, 'other': 1, 'sending': 1})
        self.assertNotIn('PRIVATE-', json.dumps(meta))

    async def test_null_and_object_shapes_and_complete_family_join_are_counted(self):
        campaign, family = await self.atomer()
        async with self.transaction() as db:
            run = await db.scalar(text('SELECT checked_run_id FROM atomer_catalog_campaigns WHERE id=:id'), {'id': campaign})
            await db.execute(text('''INSERT INTO atomer_progress_items(run_id,code,kind,state,family_key,detail)
                VALUES(:run,'SYN-CENSUS-SIBLING','standalone','linked',:family,
                       '{"private":"PRIVATE-FAMILY-DOCUMENT"}')'''), {'run': run, 'family': family})
            await db.execute(text('''INSERT INTO atomer_catalog_jobs(campaign,family_key,priority,leaf_count,guards,document)
                VALUES(:c,'s:synthetic-null',1,1,NULL,NULL),
                      (:c,'s:synthetic-json-null',1,1,'null','null')'''), {'c': campaign})
        meta = await self.inventory()
        shapes = meta['tables']['atomer_catalog_jobs']['lifecycle_shapes']
        self.assertEqual(shapes['guards_json_type'], {'missing_or_sql_null': 1, 'null': 1, 'object': 1})
        self.assertEqual(shapes['document_json_type'], {'missing_or_sql_null': 2, 'null': 1})
        self.assertEqual(meta['atomer_current_families']['count'], 2)
        self.assertEqual(meta['atomer_current_families']['matched_jobs'], 1)
        self.assertEqual(meta['atomer_current_families']['open_jobs_without_current_family'], 2)
        self.assertNotIn('PRIVATE-', json.dumps(meta))

    async def test_own_and_settled_consolidations_are_not_recounted(self):
        first = await self.preview()
        second = await self.preview()
        async with self.transaction() as db:
            await db.execute(text("UPDATE product_consolidations SET status='expired' WHERE id=:id"), {'id': second['id']})
        meta = await self.inventory(first['id'])
        self.assertEqual(meta['tables']['product_consolidations']['count'], 0)
        meta = await self.inventory()
        self.assertEqual(meta['tables']['product_consolidations']['count'], 1)

    async def test_census_is_one_repeatable_read_snapshot_even_when_new_job_arrives(self):
        campaign, _ = await self.atomer()
        original = census._buckets
        inserted = False
        async def concurrent_insert(*args, **kwargs):
            nonlocal inserted
            if not inserted:
                inserted = True
                async with self.transaction() as other:
                    await other.execute(text('''INSERT INTO atomer_catalog_jobs(campaign,family_key,priority,leaf_count)
                        VALUES(:c,'s:synthetic-late',1,1)'''), {'c': campaign})
            return await original(*args, **kwargs)
        with patch.object(census, '_buckets', side_effect=concurrent_insert):
            observed = await self.inventory()
        self.assertEqual(observed['tables']['atomer_catalog_jobs']['count'], 1)
        self.assertEqual((await self.inventory())['tables']['atomer_catalog_jobs']['count'], 2)

    async def test_diagnostic_census_never_enters_preview_or_apply_dependency_document(self):
        preview = await self.preview()
        before = await self.raw_hashes()
        report = await self.report(preview['id'])
        self.assertIn('scope_inventory', report)
        self.assertNotIn('scope_inventory', preview['dependencies'])
        self.assertEqual(await self.raw_hashes(), before)
