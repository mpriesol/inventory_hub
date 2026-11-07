"""Real PostgreSQL preservation of keeper invoices and paused receipt identity."""
import asyncio
from decimal import Decimal
import json
from pathlib import Path
import signal
import unittest
from unittest.mock import patch
from uuid import uuid4

import asyncpg
from sqlalchemy import delete, select, text

import test_product_consolidation_db as fixture
from inventory_hub import config_io
from inventory_hub.catalog_types import CatalogPrices, CatalogProduct
from inventory_hub.db_models import FeedRunStatus, IdentifierType, ProductIdentifier, Supplier, SupplierFeed, SupplierProduct
from inventory_hub.db_models_ext import ReceivingLine, ReceivingSession, StockMovement
from inventory_hub.product_consolidation_models import ProductCanonicalAlias, ProductConsolidation
from inventory_hub.product_import_models import ProductImportDraft
from inventory_hub.routers import receiving_db as receiving
from inventory_hub.services import product_consolidation as service, product_links, receiving_products, receiving_source
from inventory_hub.services import product_consolidation_keeper_dependencies as preservation
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK
from inventory_hub.supplier_prefix import SupplierPrefixError, get_supplier_prefix


@unittest.skipUnless(fixture.TEST_URL, 'Dedicated localhost *_catalog_test database required')
class KeeperDependencyDatabaseTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the installed-schema fixture without inheriting/rerunning its tests.
    transaction = fixture.ProductConsolidationDatabaseTests.transaction
    leaf = fixture.ProductConsolidationDatabaseTests.leaf
    discover = fixture.ProductConsolidationDatabaseTests.discover
    verify = fixture.ProductConsolidationDatabaseTests.verify
    absence = fixture.ProductConsolidationDatabaseTests.absence
    code_absence = fixture.ProductConsolidationDatabaseTests.code_absence
    preview = fixture.ProductConsolidationDatabaseTests.preview
    apply = fixture.ProductConsolidationDatabaseTests.apply
    stock = fixture.ProductConsolidationDatabaseTests.stock

    def request(self, *, preserve=True):
        request = fixture.ProductConsolidationDatabaseTests.request(self)
        request.preserve_keeper_invoice_receiving = preserve
        return request

    async def asyncSetUp(self):
        await fixture.ProductConsolidationDatabaseTests.asyncSetUp(self)
        config_io.save_supplier('keeper-fixture', {'product_code_prefix': 'SYN-',
            'feeds': {'sources': {'products': {'mode': 'remote', 'remote': {'url': 'https://supplier.invalid/feed'}}}}})
        async with self.transaction() as db:
            # The legacy duplicate has no valid GTIN. The receipt already
            # identifies the keeper independently, before canonicalization.
            await db.execute(delete(ProductIdentifier).where(ProductIdentifier.product_id == self.retired))
            db.add(ProductIdentifier(product_id=self.retired, identifier_type=IdentifierType.custom,
                value='SYN-LEGACY-CUSTOM'))
            supplier = Supplier(code='keeper-fixture', name='Synthetic supplier')
            db.add(supplier); await db.flush(); self.supplier_id = supplier.id
            feed = SupplierFeed(supplier_id=supplier.id, code='products', name='Products', feed_type='products',
                last_run_status=FeedRunStatus.completed, last_run_id=1)
            db.add(feed); await db.flush()
            source = CatalogProduct(supplier=supplier.code, code='KEEP', shop_code='SYN-KEEP',
                name='Synthetic retail unit', eans=[fixture.EAN], prices=CatalogPrices(currency='EUR'))
            offer = SupplierProduct(supplier_id=supplier.id, supplier_sku='KEEP', ean=fixture.EAN,
                name=source.name, source_feed_id=feed.id, attributes={'catalog': source.model_dump(mode='json')})
            db.add(offer); await db.flush(); self.source_id = offer.id
            db.add(ProductIdentifier(product_id=self.keeper, identifier_type=IdentifierType.supplier_sku,
                supplier_id=supplier.id, value='KEEP'))
            await db.execute(text('INSERT INTO product_supply_sources(product_id,supplier_product_id) VALUES(:p,:s)'),
                {'p': self.keeper, 's': offer.id})

    async def asyncTearDown(self):
        await fixture.ProductConsolidationDatabaseTests.asyncTearDown(self)

    async def documents(self):
        async with self.transaction() as db:
            session = ReceivingSession(supplier_id=self.supplier_id, warehouse_id=self.warehouse_id,
                invoice_number='SYN-INVOICE-ONE', status='paused', invoice_currency='EUR', vat_included=False,
                source_hash='a' * 64, session_data={})
            db.add(session); await db.flush(); self.receipt_id = session.id
            line = ReceivingLine(session_id=session.id, line_number=1, product_id=None,
                supplier_sku='KEEP', ean=fixture.EAN, description='Synthetic retail unit',
                ordered_qty=Decimal(3), received_qty=Decimal(3), unit_price=Decimal('2.2200'),
                purchase_to_stock_factor=Decimal(1), status='matched')
            db.add(line); await db.flush(); self.receipt_line_id = line.id
            session.session_data = {'receiving_source': {'version': 1, 'currency': 'EUR', 'vat_included': False,
                'source_hash': session.source_hash, 'rows_hash': receiving_source.rows_hash([{
                    'supplier_sku': line.supplier_sku, 'ean': line.ean, 'description': line.description,
                    'ordered_qty': line.ordered_qty, 'unit_price': line.unit_price}])}}
            self.invoice_ids, self.invoice_line_ids = [], []
            for number, quantity, receipt in [('ONE', 3, session.id), ('TWO', 4, None)]:
                identifier = await db.scalar(text('''INSERT INTO uploaded_invoices
                    (supplier_id,original_filename,stored_filename,file_path,invoice_number,receiving_session_id,
                     direction,source_kind,source_key,source_hash,document_type,is_parsed,items_count)
                    VALUES(:supplier,:name,:name,'synthetic-only',CAST(:number AS text),:receipt,'purchase','supplier',
                           CAST(:number AS text),:hash,'invoice',true,1) RETURNING id'''),
                    {'supplier': self.supplier_id, 'name': 'synthetic-' + number,
                     'number': 'SYN-INVOICE-' + number, 'receipt': receipt, 'hash': 'b' * 64})
                line_id = await db.scalar(text('''INSERT INTO uploaded_invoice_lines
                    (invoice_id,line_number,ean,supplier_sku,product_name,quantity,unit_price,matched_product_id,
                     matched_supplier_product_id,barcode_values,source_line_key,raw_data)
                    VALUES(:invoice,1,CAST(:ean AS text),'KEEP','Synthetic retail unit',:quantity,2.2200,:keeper,:source,
                           ARRAY[CAST(:ean AS text)],'synthetic-line','{}'::jsonb) RETURNING id'''),
                    {'invoice': identifier, 'ean': fixture.EAN, 'quantity': quantity,
                     'keeper': self.keeper, 'source': self.source_id})
                self.invoice_ids.append(identifier); self.invoice_line_ids.append(line_id)

    async def document_snapshot(self):
        async with self.transaction() as db:
            return {table: await db.scalar(text(f'''SELECT coalesce(string_agg(to_jsonb(t)::text,E'\\n'
                    ORDER BY to_jsonb(t)::text),'') FROM {table} t'''))
                for table in ('uploaded_invoices', 'uploaded_invoice_lines', 'receiving_sessions', 'receiving_lines',
                              'scan_events', 'receiving_scan_requests', 'invoice_payments', 'invoice_files')}

    async def test_false_and_omitted_keep_legacy_hash_replay_and_default_hold(self):
        request = self.request(preserve=False)
        original = service._request(request)
        self.assertNotIn(preservation.FLAG, original)
        request = type(request).model_validate(original)
        self.assertEqual(service._request(request), original)
        first = await self.preview(request)
        self.assertTrue(first['eligible'])
        explicit = request.model_copy(update={preservation.FLAG: False})
        self.assertEqual(await self.preview(explicit), first)
        done = await self.apply(first)
        self.assertEqual(await self.preview(explicit), done)
        self.assertNotIn('keeper_dependencies', done['dependencies'])

    async def test_default_hold_and_optin_preserve_two_invoices_and_null_paused_receipt(self):
        await self.documents()
        before, stock = await self.document_snapshot(), await self.stock()
        held = await self.preview(self.request(preserve=False))
        self.assertFalse(held['eligible'])
        self.assertIn({'code': 'consolidation_dependency_in_use', 'table': 'uploaded_invoice_lines', 'count': 2},
            held['dependencies']['blockers'])
        # Close this obsolete local preview; no product/document is changed.
        async with self.transaction() as db:
            old = await db.get(ProductConsolidation, held['id']); old.status = 'expired'
        ready = await self.preview()
        self.assertTrue(ready['eligible'], ready['dependencies']['blockers'])
        proof = ready['dependencies']['keeper_dependencies']
        self.assertEqual(proof['allowed_invoice_line_ids'], self.invoice_line_ids)
        self.assertEqual(proof['allowed_receiving_line_ids'], [self.receipt_line_id])
        self.assertIsNone(proof['proofs'][0]['stored_product_id'])
        self.assertEqual(proof['proofs'][0]['raw_product_id'], self.keeper)
        done = await self.apply(ready)
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(await self.document_snapshot(), before)
        self.assertEqual(await self.stock(), stock)
        self.assertFalse(done['result']['physical_stock_changed'])
        self.assertEqual(done['result']['keeper_dependencies_hash'], proof['preserved_rows_hash'])
        self.assertEqual(await self.apply(ready), done)

    async def test_future_reconcile_and_explicit_finalize_keep_same_identity_cost_and_uuid(self):
        await self.documents()
        invoices_before = (await self.document_snapshot())['uploaded_invoice_lines']
        stock = await self.stock()
        await self.apply(await self.preview())
        async with self.transaction() as db:
            receipt = await receiving._get_session_for_supplier(db, 'keeper-fixture', self.receipt_id, lock=True)
            self.assertIsNone(receipt.lines[0].product_id)
            await receiving_products.reconcile_products(db, receipt, 'SYN-')
            self.assertEqual(receipt.lines[0].product_id, self.keeper)
            self.assertEqual(receipt.lines[0].unit_price, Decimal('2.2200'))
            self.assertEqual(receipt.lines[0].received_qty, Decimal(3))
        self.assertEqual(await self.stock(), stock)
        async with self.transaction() as db:
            await receiving.resume_session('keeper-fixture', self.receipt_id, db)
        async with self.transaction() as db:
            done = await receiving.finalize_session('keeper-fixture', self.receipt_id,
                request=receiving.FinalizeRequest(), db=db)
        async with self.transaction() as db:
            replay = await receiving.finalize_session('keeper-fixture', self.receipt_id,
                request=receiving.FinalizeRequest(), db=db)
            movement = await db.scalar(select(StockMovement).where(
                StockMovement.idempotency_key == f'receiving:{self.receipt_id}:{self.receipt_line_id}'))
            self.assertEqual((movement.product_id, movement.quantity, movement.unit_cost),
                (self.keeper, Decimal(3), Decimal('2.2200')))
        self.assertEqual(done, replay)
        self.assertEqual((await self.document_snapshot())['uploaded_invoice_lines'], invoices_before)

    async def test_retired_invoice_fk_and_receiving_pin_never_receive_exception(self):
        await self.documents()
        async with self.transaction() as db:
            await db.execute(text('UPDATE uploaded_invoice_lines SET matched_product_id=:old WHERE id=:id'),
                {'old': self.retired, 'id': self.invoice_line_ids[0]})
            row = await db.get(ReceivingLine, self.receipt_line_id); row.product_id = self.retired
        held = await self.preview()
        self.assertFalse(held['eligible'])
        proof = held['dependencies']['keeper_dependencies']
        self.assertEqual(proof['allowed_invoice_line_ids'], [])
        self.assertEqual(proof['allowed_receiving_line_ids'], [])

    async def test_enriched_match_without_independent_raw_match_is_blocked(self):
        await self.documents()
        async with self.transaction() as db:
            row = await db.get(ReceivingLine, self.receipt_line_id)
            row.supplier_sku, row.ean = 'FEED-ONLY', None
            source = await db.get(SupplierProduct, self.source_id)
            source.supplier_sku = 'FEED-ONLY'
            source.attributes = {'catalog': {**source.attributes['catalog'], 'code': 'FEED-ONLY'}}
        held = await self.preview()
        self.assertFalse(held['eligible'])
        self.assertTrue(any(b['code'] == 'consolidation_keeper_receiving_identity_unverified'
            for b in held['dependencies']['blockers']))

    async def test_new_null_sku_row_after_preview_invalidates_scope_and_fences_apply(self):
        await self.documents()
        ready = await self.preview()
        async with self.transaction() as db:
            db.add(ReceivingLine(session_id=self.receipt_id, line_number=2, product_id=None, supplier_sku='OLD',
                ean=None, ordered_qty=1, received_qty=0, unit_price=1, status='pending'))
        with self.assertRaises(product_links.LinkError) as caught:
            await self.apply(ready)
        self.assertTrue(caught.exception.known_unapplied)
        async with self.transaction() as db:
            self.assertEqual((await db.get(ProductConsolidation, ready['id'])).status, 'expired')
            self.assertIsNone(await db.scalar(select(ProductCanonicalAlias)))

    async def test_price_or_invoice_row_change_after_preview_rolls_back(self):
        await self.documents()
        ready = await self.preview()
        async with self.transaction() as db:
            row = await db.get(ReceivingLine, self.receipt_line_id); row.unit_price = Decimal('2.2201')
        before = await self.document_snapshot()
        with self.assertRaisesRegex(product_links.LinkError, 'consolidation_dependencies_changed'):
            await self.apply(ready)
        self.assertEqual(await self.document_snapshot(), before)

    async def test_post_alias_row_change_fails_and_rolls_back_every_consolidation_write(self):
        await self.documents()
        ready = await self.preview()
        before, stock = await self.document_snapshot(), await self.stock()
        original, calls = preservation.inspect, 0
        async def inspect(db, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                await db.execute(text('UPDATE receiving_lines SET unit_price=2.2201 WHERE id=:id'),
                    {'id': self.receipt_line_id})
            return await original(db, *args, **kwargs)
        with patch.object(preservation, 'inspect', inspect):
            with self.assertRaisesRegex(product_links.LinkError, 'consolidation_keeper_dependencies_changed'):
                await self.apply(ready)
        self.assertEqual(await self.document_snapshot(), before)
        self.assertEqual(await self.stock(), stock)

    async def test_inflight_scalar_receiving_writer_fails_nowait_before_alias(self):
        await self.documents()
        ready = await self.preview()
        before = await self.document_snapshot()
        async with self.sessions() as writer:
            await writer.execute(text('UPDATE receiving_lines SET description=:value WHERE id=:id'),
                {'id': self.receipt_line_id, 'value': 'Uncommitted concurrent edit'})
            try:
                with self.assertRaisesRegex(product_links.LinkError, 'consolidation_keeper_dependency_writer_busy') as error:
                    await asyncio.wait_for(self.apply(ready), 5)
                self.assertTrue(error.exception.known_unapplied)
            finally:
                await writer.rollback()
        self.assertEqual(await self.document_snapshot(), before)
        async with self.transaction() as db:
            self.assertIsNone(await db.scalar(select(ProductCanonicalAlias)))
            self.assertEqual((await db.get(ProductConsolidation, ready['id'])).status, 'expired')

    async def test_new_null_line_cannot_enter_proof_commit_interval(self):
        await self.documents()
        ready = await self.preview()
        inspected, release, started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original, calls = preservation.inspect, 0
        async def inspect(db, *args, **kwargs):
            nonlocal calls
            proof = await original(db, *args, **kwargs)
            calls += 1
            if calls == 1:
                inspected.set()
                await asyncio.wait_for(release.wait(), 5)
            return proof
        async def writer():
            async with self.transaction() as db:
                started.set()
                await db.execute(text('''INSERT INTO receiving_lines
                    (session_id,line_number,product_id,supplier_sku,ean,ordered_qty,received_qty,unit_price,status)
                    VALUES(:session,2,NULL,'KEEP',:ean,1,0,2.2200,'pending')'''),
                    {'session': self.receipt_id, 'ean': fixture.EAN})
        with patch.object(preservation, 'inspect', inspect):
            applying = asyncio.create_task(self.apply(ready))
            writing = None
            try:
                await asyncio.wait_for(inspected.wait(), 5)
                writing = asyncio.create_task(writer())
                await asyncio.wait_for(started.wait(), 5)
                await asyncio.sleep(.04)
                self.assertFalse(writing.done())
            finally:
                release.set()
            done = await asyncio.wait_for(applying, 10)
            if writing is not None:
                await asyncio.wait_for(writing, 5)
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['dependencies']['keeper_dependencies']['scope_counts']['receiving_lines_scanned'], 1)
        async with self.transaction() as db:
            rows = list(await db.scalars(select(ReceivingLine).where(ReceivingLine.session_id == self.receipt_id)))
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row.product_id is None for row in rows))

    async def test_session_row_lock_waiting_for_identity_cannot_deadlock_consolidation(self):
        await self.documents()
        ready = await self.preview()
        locked, waiting = asyncio.Event(), asyncio.Event()
        original = preservation.lock_inputs
        async def lock_inputs(db):
            await original(db)
            locked.set()
            await asyncio.wait_for(waiting.wait(), 5)
        async def writer():
            await asyncio.wait_for(locked.wait(), 5)
            async with self.transaction() as db:
                await db.execute(select(ReceivingSession).where(ReceivingSession.id == self.receipt_id).with_for_update())
                waiting.set()
                await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
                # Models the session -> identity order of a receiving writer.
                # This executes only after consolidation's transaction commits.
                await db.execute(text('UPDATE receiving_lines SET product_id=:keeper WHERE id=:id'),
                    {'keeper': self.keeper, 'id': self.receipt_line_id})
        with patch.object(preservation, 'lock_inputs', lock_inputs):
            done, _ = await asyncio.wait_for(asyncio.gather(self.apply(ready), writer()), 10)
        self.assertEqual(done['status'], 'completed')
        self.assertIsNone(done['dependencies']['keeper_dependencies']['proofs'][0]['stored_product_id'])
        async with self.transaction() as db:
            self.assertEqual((await db.get(ReceivingLine, self.receipt_line_id)).product_id, self.keeper)

    async def test_actual_commit_holds_config_lease_without_blocking_same_event_loop(self):
        await self.documents()
        ready = await self.preview()
        before = await self.document_snapshot()
        original, calls, ticks = preservation.inspect, 0, []
        async def ticker():
            for item in range(4):
                ticks.append(item)
                await asyncio.sleep(.005)
        async def competing_reader():
            await asyncio.sleep(.002)
            with self.assertRaises(SupplierPrefixError) as error:
                config_io.load_supplier('keeper-fixture', write_back_on_load=False)
            self.assertEqual((error.exception.code, error.exception.status), ('supplier_configuration_busy', 409))
        async def inspect(db, *args, **kwargs):
            nonlocal calls
            proof = await original(db, *args, **kwargs)
            calls += 1
            if calls == 1:
                await asyncio.gather(ticker(), competing_reader())
            return proof
        def timeout(*_):
            raise TimeoutError('A configuration lock blocked the PostgreSQL commit event loop')
        previous = signal.signal(signal.SIGALRM, timeout)
        signal.setitimer(signal.ITIMER_REAL, 8)
        try:
            with patch.object(preservation, 'inspect', inspect):
                done = await asyncio.wait_for(self.apply(ready), 7)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(ticks, [0, 1, 2, 3])
        self.assertEqual(await self.document_snapshot(), before)
        self.assertEqual(get_supplier_prefix(config_io.load_supplier('keeper-fixture')), 'SYN-')

    async def test_bound_keeper_receipt_is_preserved_but_nonpaused_is_held(self):
        await self.documents()
        async with self.transaction() as db:
            line = await db.get(ReceivingLine, self.receipt_line_id); line.product_id = self.keeper
            session = await db.get(ReceivingSession, self.receipt_id); session.status = 'in_progress'
        held = await self.preview()
        self.assertFalse(held['eligible'])
        self.assertIn('consolidation_keeper_receiving_not_paused',
            {row['code'] for row in held['dependencies']['blockers']})
        async with self.transaction() as db:
            (await db.get(ProductConsolidation, held['id'])).status = 'expired'
            (await db.get(ReceivingSession, self.receipt_id)).status = 'paused'
        before = await self.document_snapshot()
        ready = await self.preview()
        self.assertTrue(ready['eligible'], ready['dependencies']['blockers'])
        await self.apply(ready)
        self.assertEqual(await self.document_snapshot(), before)

    async def test_settled_referenced_draft_with_retired_pin_remains_blocked(self):
        await self.documents()
        async with self.transaction() as db:
            draft_id = uuid4().hex
            db.add(ProductImportDraft(id=draft_id, request_hash='c' * 64, revision=1,
                supplier='keeper-fixture', shop='biketrek', status='completed',
                document={'existing_product_id': self.retired, 'code': 'SYN-OLD'}))
            session = await db.get(ReceivingSession, self.receipt_id)
            session.session_data = {**session.session_data, 'product_drafts': {str(self.receipt_line_id): draft_id}}
        held = await self.preview()
        self.assertFalse(held['eligible'])
        self.assertIn('consolidation_keeper_retired_receiving_pin',
            {row['code'] for row in held['dependencies']['blockers']})

    async def test_direct_empty_active_draft_without_product_tokens_is_held(self):
        await self.documents()
        async with self.transaction() as db:
            draft_id = uuid4().hex
            db.add(ProductImportDraft(id=draft_id, request_hash='d' * 64, revision=1,
                supplier='keeper-fixture', shop='biketrek', status='draft', document={
                    'receiving_origin': {'supplier': 'keeper-fixture', 'session_id': self.receipt_id,
                        'invoice_no': 'SYN-INVOICE-ONE'},
                    'receiving_rows': {'empty': {'line_ids': [self.receipt_line_id]}}, 'rows': []}))
            session = await db.get(ReceivingSession, self.receipt_id)
            session.session_data = {**session.session_data, 'product_drafts': {str(self.receipt_line_id): draft_id}}
        before = await self.document_snapshot()
        held = await self.preview()
        self.assertFalse(held['eligible'])
        self.assertIn('consolidation_keeper_receiving_draft_unverified',
            {row['code'] for row in held['dependencies']['blockers']})
        self.assertEqual(held['dependencies']['keeper_dependencies']['allowed_invoice_line_ids'], [])
        self.assertEqual(await self.document_snapshot(), before)

    async def test_keeper_pin_alone_without_independent_raw_identity_is_not_proof(self):
        await self.documents()
        async with self.transaction() as db:
            line = await db.get(ReceivingLine, self.receipt_line_id)
            line.product_id, line.supplier_sku, line.ean = self.keeper, None, None
            await db.execute(text('''UPDATE uploaded_invoice_lines
                SET supplier_sku=NULL,ean=NULL,barcode_values='{}',matched_supplier_product_id=NULL
                WHERE id=:id'''), {'id': self.invoice_line_ids[1]})
        held = await self.preview()
        self.assertFalse(held['eligible'])
        codes = {row['code'] for row in held['dependencies']['blockers']}
        self.assertIn('consolidation_keeper_receiving_identity_unverified', codes)
        self.assertIn('consolidation_keeper_invoice_identity_unverified', codes)

    async def test_replay_migrations_preserves_documents_and_null_receipt(self):
        await self.documents()
        await self.apply(await self.preview())
        before, stock = await self.document_snapshot(), await self.stock()
        connection = await asyncpg.connect(fixture.TEST_URL)
        try:
            await connection.execute(f'SET search_path TO "{self.schema}"')
            for number in [*range(5, 39), 46, 47, 48]:
                scripts = list(self.sql_root.glob(f'{number:03}_*.sql'))
                self.assertEqual(len(scripts), 1)
                await connection.execute(scripts[0].read_text())
        finally:
            await connection.close()
        self.assertEqual(await self.document_snapshot(), before)
        self.assertEqual(await self.stock(), stock)
