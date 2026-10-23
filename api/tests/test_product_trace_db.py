"""Product investigation never merges ambiguous barcode owners or invents stock."""
import unittest
from decimal import Decimal
from sqlalchemy import text
from fastapi import HTTPException
from invoice_db_fixture import DATABASE_REQUIRED, TEST_URL, InvoiceDatabaseCase
from inventory_hub.services.product_trace import trace


@unittest.skipUnless(TEST_URL, DATABASE_REQUIRED)
class ProductTraceDatabaseTests(InvoiceDatabaseCase):
    async def insert_invoice(self, *, code='0012345678905', product=None, quantity='2.000', number='TRACE-001'):
        async with self.sessions() as db:
            invoice = await db.scalar(text('''INSERT INTO uploaded_invoices
                (supplier_id,original_filename,stored_filename,file_path,invoice_number,is_parsed,currency)
                VALUES (:supplier,:name,:name,'/synthetic',:name,true,'EUR') RETURNING id'''),
                {'supplier': self.supplier_id,'name': number})
            await db.execute(text('''INSERT INTO uploaded_invoice_lines
                (invoice_id,line_number,ean,barcode_values,product_name,quantity,unit,unit_price,total_price,matched_product_id)
                VALUES (:invoice,1,CAST(:code AS text),ARRAY[CAST(:code AS text)],'Evidence item',:quantity,'pack',0,0,:product)'''),
                {'invoice': invoice,'code': code,'quantity': Decimal(quantity),'product': product})
            await db.commit()
            return invoice

    async def lookup(self, **kwargs):
        async with self.sessions() as db:
            return await trace(db, **kwargs)

    async def test_unlinked_invoice_barcode_is_findable_without_fabricating_card(self):
        invoice = await self.insert_invoice()
        result = await self.lookup(code='0012345678905')
        self.assertIsNone(result['product'])
        self.assertEqual(result['matches'], [])
        self.assertEqual(result['invoices']['items'][0]['invoice_id'], invoice)
        self.assertEqual(result['invoices']['items'][0]['unit_price'], '0.0000')
        self.assertEqual(result['invoices']['items'][0]['unit'], 'pack')
        self.assertEqual((await self.lookup(code='12345678905'))['invoices']['total'], 0)
        await self.assert_no_inventory_effects()

    async def test_ambiguous_scan_requires_selection_and_never_merges_other_owner(self):
        async with self.sessions() as db:
            other = await db.scalar(text("INSERT INTO products(sku,name) VALUES ('TRACE-OTHER','Other') RETURNING id"))
            for identifier in (self.product_id, other):
                await db.execute(text("INSERT INTO product_identifiers(product_id,identifier_type,value) VALUES (:id,'unverified_barcode','SHARED')"), {'id': identifier})
            await db.commit()
        own = await self.insert_invoice(code='SHARED', product=self.product_id)
        await self.insert_invoice(code='SHARED', product=other, number='TRACE-OTHER')
        result = await self.lookup(code='SHARED')
        self.assertEqual(len(result['matches']), 2)
        self.assertIsNone(result['product'])
        self.assertIn('ambiguous_code', result['warnings'])
        selected = await self.lookup(code='SHARED', product_id=self.product_id)
        self.assertEqual([row['invoice_id'] for row in selected['invoices']['items']], [own])
        with self.assertRaises(HTTPException) as raised:
            await self.lookup(code='UNKNOWN', product_id=self.product_id)
        self.assertEqual(raised.exception.status_code, 400)

    async def test_unknown_stock_and_recorded_sales_remain_distinct_from_invoices(self):
        async with self.sessions() as db:
            warehouse = await db.scalar(text("INSERT INTO warehouses(code,name) VALUES ('trace-wh','Trace warehouse') RETURNING id"))
            await db.execute(text('''INSERT INTO stock_balances(product_id,warehouse_id,qty_on_hand,qty_reserved,qty_quarantined)
                VALUES (:p,:w,50,2,1)'''), {'p': self.product_id,'w': warehouse})
            for kind, qty in [('SALE_OUT', '-3'), ('RETURN_IN', '1')]:
                await db.execute(text('''INSERT INTO stock_movements
                    (idempotency_key,product_id,warehouse_id,movement_type,quantity,balance_after,created_by)
                    VALUES (:key,:p,:w,CAST(:kind AS movement_type),:qty,0,'fixture')'''),
                    {'key': kind,'p': self.product_id,'w': warehouse,'kind': kind,'qty': Decimal(qty)})
            await db.commit()
        await self.insert_invoice(product=self.product_id, quantity='99')
        result = await self.lookup(code='ARCHIVE-1')
        self.assertEqual(result['product']['id'], self.product_id)
        self.assertFalse(result['stocks'][0]['confirmed'])
        self.assertIsNone(result['stocks'][0]['on_hand'])
        self.assertEqual(result['sales']['sold_quantity'], '3.000')
        self.assertEqual(result['sales']['returned_quantity'], '1.000')
        self.assertEqual(result['sales']['net_quantity'], '2.000')
        self.assertEqual(result['movements']['total'], 2)
        async with self.sessions() as db:
            await db.execute(text('''INSERT INTO stock_tracking
                (product_id,warehouse_id,source_type,source_id,operator_name,archived_balance)
                VALUES (:p,:w,'fixture','fixture','fixture','{}'::jsonb)'''), {'p': self.product_id,'w': warehouse})
            await db.commit()
        confirmed = await self.lookup(product_id=self.product_id)
        self.assertEqual(confirmed['stocks'][0]['available'], '47.000')
        self.assertEqual(confirmed['upgates_calls'], 0)

    async def test_invoice_results_have_complete_bounded_pagination(self):
        async with self.sessions() as db:
            invoice = await db.scalar(text('''INSERT INTO uploaded_invoices
                (supplier_id,original_filename,stored_filename,file_path,invoice_number,is_parsed)
                VALUES (:supplier,'many.pdf','many.pdf','/synthetic','MANY',true) RETURNING id'''), {'supplier': self.supplier_id})
            await db.execute(text('''INSERT INTO uploaded_invoice_lines(invoice_id,line_number,barcode_values,quantity)
                SELECT :invoice,n,ARRAY['MANY'],1 FROM generate_series(1,51) n'''), {'invoice': invoice})
            await db.commit()
        first, second = await self.lookup(code='MANY'), await self.lookup(code='MANY', invoice_page=2)
        self.assertEqual((first['invoices']['total'], len(first['invoices']['items']), len(second['invoices']['items'])), (51,50,1))
        self.assertTrue({r['line_id'] for r in first['invoices']['items']}.isdisjoint({r['line_id'] for r in second['invoices']['items']}))

    async def test_downloaded_sales_keep_units_and_credit_returns_separate_from_ledger(self):
        async with self.sessions() as db:
            other = await db.scalar(text("INSERT INTO products(sku,name) VALUES ('SALES-OTHER','Other item') RETURNING id"))
            await db.execute(text("INSERT INTO product_identifiers(product_id,identifier_type,value) VALUES (:p,'unverified_barcode','TRACE-SALES')"),
                             {'p': self.product_id})
            evidence = [
                ('sale', 'invoice', 'pack', '12', self.product_id),
                ('sale', 'credit_note', 'pack', '3', self.product_id),
                ('sale', 'invoice', 'ks', '4', self.product_id),
                ('sale', 'credit_note', 'ks', '-1', self.product_id),
                ('sale', 'invoice', 'pack', '5', None),
                ('sale', 'receipt', 'pack', '100', self.product_id),
                ('purchase', 'invoice', 'pack', '200', self.product_id),
                ('sale', 'invoice', 'pack', '999', other),
            ]
            for index, (direction, kind, unit, quantity, owner) in enumerate(evidence):
                invoice = await db.scalar(text('''INSERT INTO uploaded_invoices
                    (supplier_id,shop_id,direction,document_type,original_filename,stored_filename,
                     file_path,invoice_number,invoice_date,is_parsed)
                    VALUES (:supplier,:shop,:direction,:kind,:name,:name,'/synthetic',:name,CURRENT_DATE,true)
                    RETURNING id'''),
                    {'supplier': self.supplier_id if direction == 'purchase' else None,
                     'shop': self.shop_id if direction == 'sale' else None,
                     'direction': direction, 'kind': kind, 'name': f'SALES-{index}'})
                await db.execute(text('''INSERT INTO uploaded_invoice_lines
                    (invoice_id,line_number,ean,barcode_values,quantity,unit,matched_product_id)
                    VALUES (:invoice,1,'TRACE-SALES',ARRAY['TRACE-SALES'],:quantity,:unit,:owner)'''),
                    {'invoice': invoice, 'quantity': Decimal(quantity), 'unit': unit, 'owner': owner})
            await db.commit()

        result = await self.lookup(product_id=self.product_id)
        documents = result['sales_documents']
        self.assertEqual(documents['source'], 'shop_invoice_archive')
        quantities = {row['unit']: (row['sold_quantity'], row['returned_quantity'], row['net_quantity'])
                      for row in documents['by_unit']}
        self.assertEqual(quantities, {'pack': ('17.000', '3.000', '14.000'), 'ks': ('4.000', '1.000', '3.000')})
        monthly = {row['unit']: (row['sold'], row['returned'], row['net']) for row in documents['monthly']}
        self.assertEqual(monthly, quantities)
        self.assertEqual(result['sales']['sold_quantity'], '0')
        self.assertEqual(result['movements']['total'], 0)
        self.assertEqual(result['invoices']['total'], 7)
        await self.assert_no_inventory_effects()

    async def test_feed_only_codes_names_and_secondary_eans_keep_supplier_ownership(self):
        async with self.sessions() as db:
            for supplier in (self.supplier_id, self.other_supplier_id):
                await db.execute(text('''INSERT INTO supplier_products
                    (supplier_id,supplier_sku,manufacturer_sku,name,ean,attributes,purchase_price,purchase_currency,images)
                    VALUES (:supplier,'000-SUP','M-1','Žltý plášť 100%_','PRIMARY',
                    '{"search_name":"zlty plast 100%_","catalog":{"eans":["PRIMARY","0001234500000"]}}',7.2,'CZK',
                    '["https://images.example.test/feed.jpg"]')'''), {'supplier': supplier})
            await db.commit()
        for query in ('000-SUP', 'M-1', '0001234500000', 'zlty plast', '100%_'):
            with self.subTest(query=query):
                result = await self.lookup(code=query)
                self.assertIsNone(result['product'])
                self.assertEqual(result['matches'], [])
                self.assertEqual({row['supplier_code'] for row in result['feed_matches']['items']}, {'archive-fixture', 'archive-other'})
                self.assertTrue(all(row['hub_product_id'] is None for row in result['feed_matches']['items']))
                self.assertTrue(all(row['purchase_currency'] == 'CZK' for row in result['feed_matches']['items']))
        self.assertEqual((await self.lookup(code='1234500000'))['feed_matches']['items'], [], 'EAN leading zeros are significant')
        async with self.sessions() as db:
            await db.execute(text('''INSERT INTO product_supply_sources(product_id,supplier_product_id)
                SELECT :product,id FROM supplier_products WHERE supplier_id=:supplier'''),
                {'product': self.product_id, 'supplier': self.supplier_id})
            await db.commit()
        explicit = await self.lookup(product_id=self.product_id)
        self.assertEqual([row['hub_product_id'] for row in explicit['feed_matches']['items']], [self.product_id])
        self.assertEqual(explicit['product']['image_url'], 'https://images.example.test/feed.jpg')
        self.assertEqual(await self.scalar('SELECT count(*) FROM products'), 1)
        await self.assert_no_inventory_effects()

    async def test_shop_variant_uses_own_image_links_and_matching_price_currency(self):
        import json
        from unittest.mock import patch
        content = {'prices': [{'language': 'cs', 'price_with_vat': '999'}],
            'images': [{'url': 'https://images.example.test/parent.jpg'}],
            'variants': [{'code': 'LEAF', 'prices': [{'language': 'sk', 'price_with_vat': '12.34'}],
                         'image': 'https://images.example.test/leaf.jpg',
                         'admin_url': 'https://admin.example.test/leaf',
                         'descriptions': [{'language': 'sk', 'url': 'https://shop.example.test/leaf'}]}]}
        async with self.sessions() as db:
            await db.execute(text('''INSERT INTO shop_products
                (shop_id,product_id,external_code,parent_code,is_variant,shop_price)
                VALUES (:shop,:product,'LEAF','PARENT',true,12.34)'''),
                {'shop': self.shop_id, 'product': self.product_id})
            await db.execute(text('''INSERT INTO shop_product_content(shop_id,external_code,data)
                VALUES (:shop,'PARENT',CAST(:content AS jsonb))'''), {'shop': self.shop_id, 'content': json.dumps(content)})
            await db.commit()
        with patch('inventory_hub.services.product_trace.saved_shop_options', return_value={'languages': [{'code': 'sk', 'currency': 'EUR'}]}):
            result = await self.lookup(product_id=self.product_id)
        self.assertEqual(result['product']['image_url'], 'https://images.example.test/leaf.jpg')
        self.assertEqual(result['product']['shops'][0]['url'], 'https://shop.example.test/leaf')
        self.assertEqual(result['product']['shops'][0]['admin_url'], 'https://admin.example.test/leaf')
        price = next(row for row in result['prices'] if row['source'] == 'shop')
        self.assertEqual((price['price'], price['currency'], price['vat_included']), ('12.34', 'EUR', True))
        self.assertEqual(result['upgates_calls'], 0)
        await self.assert_no_inventory_effects()
