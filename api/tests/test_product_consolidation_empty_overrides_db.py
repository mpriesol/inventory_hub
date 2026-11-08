"""Preserve normalized empty editor rows without admitting live override data."""
import asyncio
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import test_product_consolidation_db as fixture
from inventory_hub.product_editor_types import ProductEditorSaveRequest
from inventory_hub.services import product_consolidation as service
from inventory_hub.services import product_editor, product_links as links


@unittest.skipUnless(fixture.TEST_URL, 'Dedicated localhost *_catalog_test database required')
class EmptyEditorOverrideDatabaseTests(unittest.IsolatedAsyncioTestCase):
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
    asyncSetUp = fixture.ProductConsolidationDatabaseTests.asyncSetUp
    asyncTearDown = fixture.ProductConsolidationDatabaseTests.asyncTearDown

    async def override(self, value):
        async with self.transaction() as db:
            await db.execute(text('''INSERT INTO product_editor_overrides(product_id,revision,data)
                VALUES(:id,1,CAST(:data AS jsonb)) ON CONFLICT(product_id)
                DO UPDATE SET revision=product_editor_overrides.revision+1,data=excluded.data'''),
                {'id': self.retired, 'data': json.dumps(value)})

    async def editor_rows(self):
        async with self.transaction() as db:
            return {table: await db.scalar(text(f'''SELECT coalesce(jsonb_agg(to_jsonb(t)
                ORDER BY to_jsonb(t)::text),'[]'::jsonb) FROM {table} t''')) for table in
                ('product_editor_overrides', 'product_editor_audit', 'product_editor_saves')}

    async def test_actual_editor_reset_row_and_audit_survive_consolidation_unchanged(self):
        for name in ('Synthetic manual title', None):
            async with self.transaction() as db:
                row = await product_editor.detail(db, self.retired)
                saved = await product_editor.save(db, ProductEditorSaveRequest(request_id=uuid4(), confirmed=True, changes=[{
                    'product_id': self.retired, 'expected_revision': row['revision'],
                    'snapshot_hash': row['snapshot_hash'], 'common': {'name': name}}]))
                self.assertEqual(saved['results'][0]['status'], 'saved')
        before, stock = await self.editor_rows(), await self.stock()
        self.assertEqual(before['product_editor_overrides'][0]['data'], {})
        self.assertEqual(len(before['product_editor_audit']), 2)
        preview = await self.preview()
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        facts = preview['dependencies']['facts']
        self.assertEqual(facts['product_editor_overrides.product_id']['count'], 1)
        self.assertEqual(facts['preserved_empty_editor_overrides'], facts['product_editor_overrides.product_id'])
        self.assertEqual((await self.apply(preview))['status'], 'completed')
        self.assertEqual(before, await self.editor_rows())
        self.assertEqual(stock, await self.stock())

    async def test_nonempty_hidden_scopes_and_unknown_keys_remain_blocked(self):
        for value in ({'common': {'name': 'Manual'}}, {'common': {'name': None}},
                {'variant': {'sale_price_gross': '0'}}, {'shops': {'biketrek': {'visible': False}}},
                {'warehouses': {'hlavny-sklad': {'location': 'A1'}}},
                {'published': {'biketrek': {'name': {'value': 'Synthetic'}}}},
                {'future_scope': {}}, {'common': {}}):
            with self.subTest(document=value):
                await self.override(value)
                preview = await self.preview()
                self.assertFalse(preview['eligible'])
                self.assertTrue(any(row['code'] == 'consolidation_dependency_in_use'
                    and row.get('table') == 'product_editor_overrides' and row.get('count') == 1
                    for row in preview['dependencies']['blockers']))
                self.assertNotIn('preserved_empty_editor_overrides', preview['dependencies']['facts'])

    async def test_malformed_documents_and_metadata_fail_closed_in_dependency_inventory(self):
        # Exercise the dependency guard directly: the existing display-context
        # reader rejects malformed override shapes before preview can reach it.
        raw = service._request(self.request())
        async with self.transaction() as db:
            local = await service._local(db, raw)
            await db.execute(text('''ALTER TABLE product_editor_overrides
                DROP CONSTRAINT product_editor_overrides_revision_check,
                ALTER COLUMN revision DROP NOT NULL, ALTER COLUMN data DROP NOT NULL,
                ALTER COLUMN updated_at DROP NOT NULL'''))
        for statement in (
                "data='null'::jsonb", "data='[]'::jsonb", "data='0'::jsonb",
                "data='false'::jsonb", "data=jsonb_build_object('common',NULL)", 'data=NULL',
                'revision=0', 'revision=-1', 'revision=NULL', 'updated_at=NULL'):
            with self.subTest(statement=statement):
                async with self.transaction() as db:
                    await db.execute(text('''INSERT INTO product_editor_overrides(product_id,revision,data)
                        VALUES(:id,1,'{}') ON CONFLICT(product_id)
                        DO UPDATE SET revision=1,data='{}',updated_at=now()'''), {'id': self.retired})
                    await db.execute(text(f'UPDATE product_editor_overrides SET {statement} WHERE product_id=:id'),
                                     {'id': self.retired})
                async with self.transaction() as db:
                    await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'))
                    result = await service._dependencies(db, raw, local)
                self.assertTrue(any(row['code'] == 'consolidation_dependency_in_use'
                    and row.get('table') == 'product_editor_overrides' and row.get('count') == 1
                    for row in result['blockers']))
                self.assertNotIn('preserved_empty_editor_overrides', result['facts'])

    async def test_unknown_table_column_prevents_empty_row_exemption(self):
        await self.override({})
        async with self.transaction() as db:
            await db.execute(text('ALTER TABLE product_editor_overrides ADD COLUMN future_intent jsonb'))
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        self.assertTrue(any(row.get('table') == 'product_editor_overrides'
            and row['code'] == 'consolidation_dependency_in_use' for row in preview['dependencies']['blockers']))
        self.assertNotIn('preserved_empty_editor_overrides', preview['dependencies']['facts'])

    async def test_disabled_retired_reference_guard_still_blocks_empty_row(self):
        await self.override({})
        async with self.transaction() as db:
            await db.execute(text('ALTER TABLE product_editor_overrides DISABLE TRIGGER trg_retired_reference_product_id'))
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        self.assertTrue(any(row.get('table') == 'product_editor_overrides'
            and row['code'] == 'consolidation_reference_guard_missing' for row in preview['dependencies']['blockers']))

    async def test_changed_empty_revision_or_new_value_rejects_stale_apply(self):
        for value in ({}, {'common': {'name': 'New manual title'}}):
            with self.subTest(document=value):
                await self.override({})
                preview = await self.preview()
                self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
                stock = await self.stock()
                await self.override(value)
                expected = 'consolidation_context_changed' if value else 'consolidation_dependencies_changed'
                with self.assertRaisesRegex(links.LinkError, expected) as caught:
                    await self.apply(preview)
                self.assertTrue(caught.exception.known_unapplied)
                self.assertEqual(stock, await self.stock())
                async with self.transaction() as db:
                    self.assertEqual(await db.scalar(text('SELECT count(*) FROM product_canonical_aliases')), 0)

    async def test_empty_row_deletion_or_insertion_invalidates_stale_review(self):
        await self.override({})
        stock = await self.stock()
        for inserting in (False, True):
            with self.subTest(inserting=inserting):
                preview = await self.preview()
                self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
                if inserting:
                    await self.override({})
                else:
                    async with self.transaction() as db:
                        await db.execute(text('DELETE FROM product_editor_overrides WHERE product_id=:id'),
                                         {'id': self.retired})
                with self.assertRaisesRegex(links.LinkError, 'consolidation_dependencies_changed') as caught:
                    await self.apply(preview)
                self.assertTrue(caught.exception.known_unapplied)
        self.assertEqual(stock, await self.stock())

    async def race(self, statement, *, existing=True):
        if existing:
            await self.override({})
        preview = await self.preview()
        before, stock = await self.editor_rows(), await self.stock()
        scanned, finish, writer_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        writer_pid = None
        inspect = service._dependencies
        async def observe(*args, **kwargs):
            result = await inspect(*args, **kwargs)
            scanned.set()
            await finish.wait()
            return result
        async def write():
            nonlocal writer_pid
            async with self.transaction() as db:
                writer_pid = await db.scalar(text('SELECT pg_backend_pid()'))
                writer_started.set()
                await db.execute(text(statement), {'id': self.retired})
        with patch.object(service, '_dependencies', side_effect=observe):
            applying = asyncio.create_task(self.apply(preview))
            await asyncio.wait_for(scanned.wait(), 5)
            writing = asyncio.create_task(write())
            try:
                await asyncio.wait_for(writer_started.wait(), 5)
                async with self.transaction() as db:
                    async with asyncio.timeout(3):
                        while not await db.scalar(text('SELECT EXISTS(SELECT 1 FROM pg_locks '
                                                       'WHERE pid=:pid AND NOT granted)'), {'pid': writer_pid}):
                            self.assertFalse(writing.done())
                            await asyncio.sleep(.01)
                self.assertFalse(writing.done())
            finally:
                finish.set()
                outcomes = await asyncio.wait_for(asyncio.gather(applying, writing, return_exceptions=True), 10)
            self.assertEqual(outcomes[0]['status'], 'completed')
            self.assertIsInstance(outcomes[1], IntegrityError)
            self.assertIn('product_retired', str(outcomes[1]))
        self.assertEqual(before, await self.editor_rows())
        self.assertEqual(stock, await self.stock())

    async def test_override_update_after_final_scan_waits_and_rejects_retired_id(self):
        await self.race('''UPDATE product_editor_overrides
            SET data='{"common":{"name":"Late manual title"}}' WHERE product_id=:id''')

    async def test_override_delete_after_final_scan_waits_and_rejects_retired_id(self):
        await self.race('DELETE FROM product_editor_overrides WHERE product_id=:id')

    async def test_override_insert_after_final_scan_waits_and_rejects_retired_id(self):
        await self.race("INSERT INTO product_editor_overrides(product_id,revision,data) VALUES(:id,1,'{}')",
                        existing=False)
