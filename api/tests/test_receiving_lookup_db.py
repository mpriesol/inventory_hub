"""Scanner discovery is read-only and includes completed, active and new invoices."""
import unittest
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4
from sqlalchemy import select, func
import test_receiving_db as fixture
from inventory_hub.db_models import ReceivingStatus, SupplierProduct
from inventory_hub.db_models_ext import ReceivingSession, StockMovement
from inventory_hub.routers import receiving_db as receiving
from inventory_hub.services import receiving_lookup as service


@unittest.skipUnless(fixture.TEST_URL, 'Requires isolated localhost *_catalog_test database')
class ReceivingLookupDatabaseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ReceivingDatabaseTests.asyncSetUp
    asyncTearDown = fixture.ReceivingDatabaseTests.asyncTearDown
    make_session = fixture.ReceivingDatabaseTests.make_session

    async def test_find_all_states_and_source_ean_without_mutating_receipts(self):
        first = await self.make_session([{'supplier_sku':'catalog-only'}])
        second = await self.make_session([{'ean':'5901234123457','supplier_sku':'other'}])
        async with self.sessions() as db:
            (await db.get(ReceivingSession, second)).status = ReceivingStatus.completed
            db.add(SupplierProduct(supplier_id=self.supplier_id, supplier_sku='catalog-only', name='Synthetic',ean='5901234123457'))
            await db.commit()
        row = {'supplier_sku':'new-item','ean':'5901234123457','description':'Synthetic new','ordered_qty':Decimal(2),'unit_price':Decimal(1)}
        with patch.object(service, '_files', return_value=([('receipt-test','not-started',[row])],0)):
            async with self.sessions() as db:
                result = await service.lookup(db, '5901234123457')
                self.assertEqual(len(result['candidates']),3)
                self.assertIn('completed', [candidate['status'] for candidate in result['candidates']])
                self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)),0)
        async with self.sessions() as db:
            self.assertEqual((await db.get(ReceivingSession, second)).status, ReceivingStatus.completed)

    async def test_catalog_only_barcode_scan_counts_once_with_original_uuid(self):
        async with self.sessions() as db:
            db.add(SupplierProduct(supplier_id=self.supplier_id, supplier_sku='catalog-only',name='Synthetic',ean='5901234123457'))
            await db.commit()
        session_id = await self.make_session([{'supplier_sku':'catalog-only'}])
        request = receiving.ScanRequest(code='5901234123457',request_id=uuid4())
        async with self.sessions() as db:
            first = await receiving.scan_code('receipt-test',session_id,request,db); await db.commit()
        async with self.sessions() as db:
            again = await receiving.scan_code('receipt-test',session_id,request,db); await db.commit()
        self.assertEqual(first['line']['received_qty'],1)
        self.assertTrue(again['replayed'])
