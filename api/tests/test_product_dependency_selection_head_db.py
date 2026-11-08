"""One-round-trip selection admission preserves complete, bounded proofs."""
import unittest
from unittest.mock import patch

from sqlalchemy import event, text

import test_product_consolidation_db as fixture
import test_product_dependency_linked_reuse_db as linked_tests
from inventory_hub.services import product_dependency_inventory as inventory
from inventory_hub.services import product_dependency_scope as scope
from inventory_hub.services.product_dependency_reuse import ResolutionMemo


@unittest.skipUnless(fixture.TEST_URL, 'Dedicated localhost *_catalog_test database required')
class SelectionHeadDatabaseTests(unittest.IsolatedAsyncioTestCase):
    transaction = linked_tests.LinkedInputReuseDatabaseTests.transaction
    leaf = linked_tests.LinkedInputReuseDatabaseTests.leaf
    discover = linked_tests.LinkedInputReuseDatabaseTests.discover
    verify = linked_tests.LinkedInputReuseDatabaseTests.verify
    absence = linked_tests.LinkedInputReuseDatabaseTests.absence
    code_absence = linked_tests.LinkedInputReuseDatabaseTests.code_absence
    request = linked_tests.LinkedInputReuseDatabaseTests.request
    current = linked_tests.LinkedInputReuseDatabaseTests.current
    atomer = linked_tests.LinkedInputReuseDatabaseTests.atomer
    families = linked_tests.LinkedInputReuseDatabaseTests.families
    asyncSetUp = linked_tests.LinkedInputReuseDatabaseTests.asyncSetUp
    asyncTearDown = linked_tests.LinkedInputReuseDatabaseTests.asyncTearDown

    async def test_full_proof_and_tail_reference_match_with_fewer_queries(self):
        await self.families(25, 3, tail_reference=True)
        observed = []
        def query(*args):
            observed.append(args[2])
        event.listen(self.engine.sync_engine, 'before_cursor_execute', query)
        try:
            async def metadata_only(db, table, condition, params, **kwargs):
                return await inventory.statistics(db, table, condition, params), None
            with patch.object(inventory, 'selection_head', metadata_only):
                previous = await self.current()
            previous_queries = len(observed)
            observed.clear()
            current = await self.current()
            current_queries = len(observed)
        finally:
            event.remove(self.engine.sync_engine, 'before_cursor_execute', query)
        for result in (previous, current):
            result.pop('work')
        self.assertEqual(previous, current)
        self.assertTrue(current['complete'])
        self.assertEqual(current['classification_counts']['atomer_catalog_jobs']['reference'], 1)
        self.assertLessEqual(current_queries, previous_queries - 25)

    async def test_byte_prefix_and_composite_cursor_never_skip_an_unfetched_row(self):
        campaign = await self.families(7, 3, padding=100)
        results = []
        async with self.transaction() as db:
            await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'))
            run = await db.scalar(text('SELECT checked_run_id FROM atomer_catalog_campaigns WHERE id=:id'), {'id': campaign})
            condition = "t.run_id=:run AND t.family_key LIKE 's:synthetic-page-%'"
            for page_rows, page_bytes in ((1, 1), (7, 1), (64, 500), (64, 4000000)):
                budget = inventory.Budget(16000000, 64000000, 256000000, None)
                closure = inventory.Closure(db, budget=budget, page_rows=page_rows,
                    page_bytes=page_bytes, retained=0, max_linked_rows=20000)
                rows = await closure.rows('synthetic', 'atomer_progress_items', condition, {'run': run})
                results.append((rows, closure.result(), budget.rows, budget.bytes))
                self.assertEqual(len(rows), 21)
                self.assertEqual(len({tuple(row['key']) for row in rows}), 21)
                self.assertLessEqual(budget.peak_payload, budget.max_payload)
        self.assertTrue(all(result == results[0] for result in results))

    async def test_inadmissible_whole_selection_never_returns_bodies(self):
        await self.families(3, 2, padding=2000)
        async with self.transaction() as db:
            condition = "t.family_key LIKE 's:synthetic-page-%'"
            for max_row, max_payload, max_linked in ((100, 64000000, 20000),
                                                    (16000000, 100, 20000),
                                                    (16000000, 64000000, 5)):
                budget = inventory.Budget(max_row, max_payload, 256000000, None)
                facts, rows = await inventory.selection_head(db, 'atomer_progress_items', condition, {},
                    budget=budget, page_rows=64, page_bytes=4000000, retained=0,
                    max_linked_rows=max_linked)
                self.assertEqual(facts['count'], 6)
                self.assertEqual(rows, [])
                self.assertEqual(budget.rows, 0)
                self.assertLessEqual(budget.peak_payload, max_payload)

    async def test_work_exhaustion_gates_bodies_and_keeps_original_hold(self):
        await self.families(1, 2, padding=200)
        async with self.transaction() as db:
            condition = "t.family_key LIKE 's:synthetic-page-%'"
            budget = inventory.Budget(16000000, 64000000, 1, None)
            facts, rows = await inventory.selection_head(db, 'atomer_progress_items', condition, {},
                budget=budget, page_rows=64, page_bytes=4000000, retained=0, max_linked_rows=20000)
            self.assertEqual(facts['count'], 2)
            self.assertTrue(all(row['document'] is None for row in rows))
            closure = inventory.Closure(db, budget=budget, page_rows=64, page_bytes=4000000,
                retained=0, max_linked_rows=20000)
            with self.assertRaisesRegex(scope.Unverified, 'database_total_document_limit'):
                await closure.rows('synthetic', 'atomer_progress_items', condition, {})

    async def test_empty_selection_and_first_row_larger_than_page_are_complete(self):
        await self.families(1, 1, padding=2000)
        async with self.transaction() as db:
            for condition, count in (("t.family_key='missing'", 0),
                                     ("t.family_key LIKE 's:synthetic-page-%'", 1)):
                budget = inventory.Budget(16000000, 64000000, 256000000, None)
                closure = inventory.Closure(db, budget=budget, page_rows=64, page_bytes=1,
                    retained=0, max_linked_rows=20000)
                rows = await closure.rows('synthetic', 'atomer_progress_items', condition, {})
                self.assertEqual(len(rows), count)
                self.assertEqual(budget.rows, count)
                self.assertEqual(closure.result()['edges'], count)
                self.assertEqual(budget.peak_payload, sum(row['bytes'] for row in rows))

    async def test_tight_prefetch_uses_metadata_without_evicting_cached_evidence(self):
        await self.families(1, 1, padding=2000)
        async with self.transaction() as db:
            memo = ResolutionMemo(16000, sort_keys=False)
            budget = inventory.Budget(16000, 20000, 256000000, None, linked_reuse=memo)
            memo.put(('unrelated',), {'body': 'x' * 15000})
            budget.account_reuse()
            self.assertGreater(memo.bytes, 10000)
            facts, rows = await inventory.selection_head(db, 'atomer_progress_items',
                "t.family_key LIKE 's:synthetic-page-%'", {}, budget=budget, page_rows=64,
                page_bytes=4000, retained=1000, max_linked_rows=20000)
            self.assertEqual(facts['count'], 1)
            self.assertIsNone(rows)
            self.assertEqual(memo.evictions, 0)
            self.assertEqual(memo.get(('unrelated',)), {'body': 'x' * 15000})
            self.assertLessEqual(budget.retained + memo.bytes, budget.max_payload)

    async def test_empty_miss_near_work_limit_preserves_later_cache_hit(self):
        await self.families(1, 1, padding=4000)
        async def sequence(metadata_only):
            memo = ResolutionMemo(16000, sort_keys=False)
            budget = inventory.Budget(16000, 20000, 256000, None, linked_reuse=memo)
            async with self.transaction() as db:
                async def read(condition, retained=0):
                    closure = inventory.Closure(db, budget=budget, page_rows=64, page_bytes=4000,
                        retained=retained, max_linked_rows=20000)
                    result = await closure.rows('synthetic', 'atomer_progress_items', condition, {})
                    return result, closure.result()
                async def old(db, table, condition, params, **kwargs):
                    return await inventory.statistics(db, table, condition, params), None
                from contextlib import nullcontext
                with patch.object(inventory, 'selection_head', old) if metadata_only else nullcontext():
                    first = await read("t.family_key LIKE 's:synthetic-page-%'")
                    charged = budget.bytes
                    budget.max_work = charged + 500
                    # Enough for the empty memo entry, less than the 500-byte
                    # speculative prefix; the exact empty read keeps both.
                    await read("t.family_key='missing'", retained=budget.max_payload - memo.bytes - 300)
                    repeated = await read("t.family_key LIKE 's:synthetic-page-%'")
                self.assertEqual(first, repeated)
                self.assertEqual(budget.bytes, charged)
                self.assertGreater(memo.hits, 0)
                return first, budget.bytes, budget.rows, memo.evictions
        self.assertEqual(await sequence(True), await sequence(False))

    async def test_time_and_invalid_page_guards_remain_before_query(self):
        async with self.transaction() as db:
            budget = inventory.Budget(16000, 64000, 256000, -1)
            with self.assertRaisesRegex(TimeoutError, 'database_inventory_time_limit'):
                await inventory.selection_head(db, 'shops', 'true', {}, budget=budget,
                    page_rows=64, page_bytes=4000, retained=0, max_linked_rows=20000)
            with self.assertRaisesRegex(scope.Unverified, 'invalid_inventory_page_budget'):
                await inventory.selection_head(db, 'shops', 'true', {}, budget=budget,
                    page_rows=0, page_bytes=4000, retained=0, max_linked_rows=20000)

    async def test_late_query_completion_reports_already_decoded_prefix(self):
        async with self.transaction() as db:
            budget = inventory.Budget(16000, 64000, 256000, None)
            class LateQuery:
                async def execute(self, statement, params):
                    result = await db.execute(statement, params)
                    budget.max_seconds = -1
                    return result
            with self.assertRaisesRegex(TimeoutError, 'database_inventory_time_limit'):
                await inventory.selection_head(LateQuery(), 'shops', 'true', {}, budget=budget,
                    page_rows=64, page_bytes=4000, retained=0, max_linked_rows=20000)
            self.assertGreater(budget.peak_payload, 0)
            self.assertLessEqual(budget.peak_payload, budget.max_payload)
