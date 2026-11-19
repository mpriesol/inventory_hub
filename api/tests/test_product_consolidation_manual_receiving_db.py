"""New manual receipt references keep consolidation's fail-closed protection."""
import unittest
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import test_product_consolidation_db as fixture
from inventory_hub.services import product_links as links


@unittest.skipUnless(fixture.TEST_URL, 'Dedicated localhost *_catalog_test database required')
class ManualReceivingDependencyTests(unittest.IsolatedAsyncioTestCase):
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

    async def manual_scan(self, product_id):
        batch_id, scan_id = uuid4(), uuid4()
        async with self.transaction() as db:
            await db.execute(text('''INSERT INTO mixed_receiving_batches(id,warehouse_id,actor)
                VALUES(:id,:warehouse,'synthetic operator')'''),
                {'id': batch_id, 'warehouse': self.warehouse_id})
            await db.execute(text('''INSERT INTO mixed_receiving_manual_scans
                (id,batch_id,product_id,code,quantity,stock_unit,cost_status,reason)
                VALUES(:id,:batch,:product,'SYN-OLD',1,'ks','unknown','Synthetic missing invoice')'''),
                {'id': scan_id, 'batch': batch_id, 'product': product_id})
        return scan_id

    async def test_empty_manual_table_allows_consolidation_and_retired_writes_are_rejected(self):
        before = await self.stock()
        preview = await self.preview()
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        self.assertEqual((await self.apply(preview))['status'], 'completed')
        with self.assertRaisesRegex(IntegrityError, 'product_retired'):
            await self.manual_scan(self.retired)
        scan = await self.manual_scan(self.keeper)
        with self.assertRaisesRegex(IntegrityError, 'product_retired'):
            async with self.transaction() as db:
                await db.execute(text('UPDATE mixed_receiving_manual_scans SET product_id=:product WHERE id=:id'),
                    {'product': self.retired, 'id': scan})
        self.assertEqual(before, await self.stock())

    async def test_active_and_cancelled_manual_references_block_without_rewriting_history(self):
        await self.manual_scan(self.retired)
        before = await self.stock()
        for status in ('active', 'cancelled'):
            with self.subTest(status=status):
                async with self.transaction() as db:
                    await db.execute(text('UPDATE mixed_receiving_manual_scans SET status=:status'), {'status': status})
                    evidence = await db.scalar(text('''SELECT jsonb_agg(to_jsonb(t))
                        FROM mixed_receiving_manual_scans t'''))
                preview = await self.preview()
                self.assertFalse(preview['eligible'])
                self.assertTrue(any(blocker['code'] == 'consolidation_dependency_in_use'
                    and blocker.get('table') == 'mixed_receiving_manual_scans'
                    for blocker in preview['dependencies']['blockers']))
                with self.assertRaisesRegex(links.LinkError, 'consolidation_dependencies_blocked'):
                    await self.apply(preview)
                async with self.transaction() as db:
                    self.assertEqual(evidence, await db.scalar(text('''SELECT jsonb_agg(to_jsonb(t))
                        FROM mixed_receiving_manual_scans t''')))
                    self.assertEqual(await db.scalar(text('SELECT count(*) FROM product_canonical_aliases')), 0)
                self.assertEqual(before, await self.stock())

    async def test_missing_manual_reference_guard_still_fails_closed(self):
        async with self.transaction() as db:
            await db.execute(text('''ALTER TABLE mixed_receiving_manual_scans
                DISABLE TRIGGER trg_retired_reference_product_id'''))
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        self.assertIn({'code': 'consolidation_reference_guard_missing', 'table': 'mixed_receiving_manual_scans'},
            preview['dependencies']['blockers'])
