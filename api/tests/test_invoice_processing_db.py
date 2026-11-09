"""Invoice handling is reversible without changing stock, original files or money."""
import asyncio
import json
from pathlib import Path
import unittest
from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from invoice_db_fixture import DATABASE_REQUIRED, TEST_URL, InvoiceDatabaseCase
from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.invoice_processing_types import InvoiceProcessingApply, InvoiceProcessingPreview
from inventory_hub.routers import invoices_unified as routes
from inventory_hub.services import invoice_archive as archive
from inventory_hub.services import invoice_calendar, invoice_finance as finance
from inventory_hub.services import invoice_processing as service


@unittest.skipUnless(TEST_URL, DATABASE_REQUIRED)
class InvoiceProcessingDatabaseTests(InvoiceDatabaseCase):
    async def invoice(self, *, direction='purchase', paid=None, source_data=None):
        source = dict(direction=direction, source_kind='supplier' if direction == 'purchase' else 'shop',
            source_key=uuid4().hex,
            metadata=dict(invoice_number='DOC-' + uuid4().hex, invoice_date='2026-01-01',
                          due_date='2026-01-15', currency='EUR', total_with_vat='100.00', source_paid_amount=paid),
            files=[dict(filename='original.pdf', content=b'%PDF-1.4\nSynthetic original\n%%EOF', role='original', file_type='pdf')],
            source_data=source_data or {})
        source['supplier_id' if direction == 'purchase' else 'shop_id'] = self.supplier_id if direction == 'purchase' else self.shop_id
        async with self.sessions() as db:
            result = await archive.ingest_invoice(db, **source)
            await db.commit()
        return result['id'], source

    async def receipt(self, source, *, status='in_progress', data=None, supplier_id=None):
        async with self.sessions() as db:
            warehouse = await db.scalar(text("INSERT INTO warehouses(code,name) VALUES(:code,'Synthetic warehouse') RETURNING id"),
                                        {'code': 'INV-' + uuid4().hex})
            identifier = await db.scalar(text('''INSERT INTO receiving_sessions
                (supplier_id,warehouse_id,invoice_number,status,session_data)
                VALUES(:supplier,:warehouse,:number,CAST(:status AS receiving_status),CAST(:data AS jsonb)) RETURNING id'''),
                {'supplier': supplier_id or self.supplier_id, 'warehouse': warehouse,
                 'number': source['metadata']['invoice_number'], 'status': status, 'data': json.dumps(data or {})})
            await db.commit()
        return identifier

    async def change_receipt(self, identifier, status, data):
        async with self.sessions() as db:
            await db.execute(text('UPDATE receiving_sessions SET status=CAST(:status AS receiving_status),session_data=CAST(:data AS jsonb),updated_at=NOW() WHERE id=:id'),
                             {'id': identifier, 'status': status, 'data': json.dumps(data)})
            await db.commit()

    async def current(self, identifier):
        async with self.sessions() as db:
            row = (await db.execute(text('SELECT * FROM uploaded_invoices WHERE id=:id'), {'id': identifier})).mappings().one()
            return await service.detail(db, row)

    async def reviewed(self, identifier, action='process_without_stock', *, note='Explicit synthetic reason', revision=None):
        if revision is None:
            revision = (await self.current(identifier))['revision']
        request = InvoiceProcessingPreview(action=action, expected_revision=revision, note=note)
        async with self.sessions() as db:
            result = await service.preview(db, identifier, request)
        return InvoiceProcessingApply(**request.model_dump(), request_id=uuid4(), preview_hash=result['preview_hash'], confirmed=True)

    async def apply(self, identifier, request):
        async with self.sessions() as db:
            result = await service.apply(db, identifier, request, actor='fixture-operator')
            await db.commit()
            return result

    async def perform(self, identifier, action, **changes):
        return await self.apply(identifier, await self.reviewed(identifier, action, **changes))

    async def expect_preview_error(self, code, identifier, action):
        with self.assertRaises(service.InvoiceProcessingError) as error:
            await self.reviewed(identifier, action)
        self.assertEqual(error.exception.code, code)

    async def test_process_reopen_archive_restore_preserve_unknown_money_and_original_bytes(self):
        identifier, source = await self.invoice()
        originals = await self.rows('SELECT file_path,sha256 FROM invoice_files WHERE invoice_id=:id', {'id': identifier})
        original_bytes = [Path(item['file_path']).read_bytes() for item in originals]
        request = await self.reviewed(identifier, note='  Checked paperwork only  ')
        self.assertEqual(await self.scalar('SELECT count(*) FROM invoice_processing_operations'), 0)
        self.assertEqual((await self.current(identifier))['revision'], 0, 'A preview is read-only')
        processed = await self.apply(identifier, request)
        self.assertEqual(processed['processing']['status'], 'processed_without_stock')
        self.assertEqual((processed['stock_effect'], processed['payment_effect']), (False, False))
        await self.perform(identifier, 'reopen', note='Document needs review again')
        await self.perform(identifier, 'archive', note='Retire synthetic test document')
        async with self.sessions() as db:
            replay = await archive.ingest_invoice(db, **source)
            await db.commit()
        self.assertTrue(replay['replayed'])
        archived = await self.current(identifier)
        self.assertTrue(archived['archived'], 'Archive replay must not resurrect a retired invoice')
        self.assertEqual(archived['status'], 'unprocessed')
        restored = await self.perform(identifier, 'restore', note='Restore the same original')
        self.assertFalse(restored['processing']['archived'])
        current = await self.current(identifier)
        self.assertEqual(current['revision'], 4)
        self.assertEqual([row['action'] for row in current['history']], ['restore', 'archive', 'reopen', 'process_without_stock'])
        self.assertEqual(current['history'][-1]['note'], 'Checked paperwork only')
        self.assertTrue(all(row['actor'] == 'fixture-operator' for row in current['history']))
        self.assertEqual(await self.rows('SELECT file_path,sha256 FROM invoice_files WHERE invoice_id=:id', {'id': identifier}), originals)
        self.assertEqual([Path(item['file_path']).read_bytes() for item in originals], original_bytes)
        async with self.sessions() as db:
            row = (await db.execute(text('SELECT * FROM uploaded_invoices WHERE id=:id'), {'id': identifier})).mappings().one()
            amounts = await finance.detail(db, row)
        self.assertEqual((amounts['paid_amount'], amounts['status'], amounts['opening_required']), (None, 'unknown', True))
        self.assertEqual(await self.scalar('SELECT count(*) FROM invoice_payments'), 0)
        await self.assert_no_inventory_effects()

    async def test_exact_uuid_replays_after_newer_actions_and_rejects_changed_payload_or_invoice(self):
        identifier, _ = await self.invoice()
        other, _ = await self.invoice()
        request = await self.reviewed(identifier)
        first = await self.apply(identifier, request)
        await self.perform(identifier, 'reopen')
        self.assertEqual(await self.apply(identifier, request), first)
        async with self.sessions() as db:
            self.assertEqual(await service.operation_result(db, identifier, request.request_id), first)
            with self.assertRaises(service.InvoiceProcessingError) as error:
                await service.operation_result(db, other, request.request_id)
            self.assertEqual(error.exception.code, 'invoice_processing_operation_not_found')
        for target, changed in ((other, request), (identifier, request.model_copy(update={'note': 'Changed reason'}))):
            with self.subTest(target=target), self.assertRaises(service.InvoiceProcessingError) as error:
                await self.apply(target, changed)
            self.assertEqual(error.exception.code, 'invoice_processing_request_conflict')
        self.assertEqual(await self.scalar('SELECT count(*) FROM invoice_processing_operations'), 2)

    async def test_concurrent_same_request_is_once_and_distinct_requests_have_one_cas_winner(self):
        identifier, _ = await self.invoice()
        request = await self.reviewed(identifier)
        results = await asyncio.gather(self.apply(identifier, request), self.apply(identifier, request))
        self.assertEqual(results[0], results[1])
        self.assertEqual(await self.scalar('SELECT count(*) FROM invoice_processing_operations'), 1)
        first = await self.reviewed(identifier, 'reopen')
        second = await self.reviewed(identifier, 'reopen', note='Another operator correction')
        results = await asyncio.gather(self.apply(identifier, first), self.apply(identifier, second), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1, results)
        self.assertEqual([result.code for result in results if isinstance(result, service.InvoiceProcessingError)], ['invoice_processing_changed'])
        self.assertEqual(await self.scalar('SELECT count(*) FROM invoice_processing_operations'), 2)

    async def test_rollback_leaves_no_partial_state_or_uuid_and_exact_request_can_retry(self):
        identifier, _ = await self.invoice()
        request = await self.reviewed(identifier)
        async with self.sessions() as db:
            await service.apply(db, identifier, request, actor='fixture-operator')
            await db.rollback()
        self.assertEqual((await self.current(identifier))['revision'], 0)
        self.assertEqual(await self.scalar('SELECT count(*) FROM invoice_processing_operations'), 0)
        self.assertEqual((await self.apply(identifier, request))['processing_revision'], 1)
        await self.assert_no_inventory_effects()

    async def test_receipts_found_without_legacy_invoice_link_block_manual_labels_and_archive(self):
        identifier, source = await self.invoice()
        session_id = await self.receipt(source)
        self.assertIsNone(await self.scalar('SELECT receiving_session_id FROM uploaded_invoices WHERE id=:id', {'id': identifier}))
        current = await self.current(identifier)
        self.assertEqual(current['status'], 'receiving')
        self.assertEqual(current['receipts'][0]['id'], session_id)
        await self.expect_preview_error('invoice_processing_receiving_active', identifier, 'process_without_stock')
        await self.expect_preview_error('invoice_processing_archive_receipt_first', identifier, 'archive')
        await self.change_receipt(session_id, 'completed', {'finalize_result': {'session_id': session_id}})
        async with self.sessions() as db:
            await db.execute(text('''INSERT INTO stock_movements
                (idempotency_key,product_id,warehouse_id,movement_type,quantity,unit_cost,reference_type,reference_id,balance_after,avg_cost_after)
                SELECT :key,:product,warehouse_id,'RECEIVING_IN',2,4,'receiving_session',id::text,2,4
                FROM receiving_sessions WHERE id=:session'''),
                {'key': 'synthetic-receipt-' + str(session_id), 'product': self.product_id, 'session': session_id})
            await db.commit()
        original_movements = await self.rows('SELECT * FROM stock_movements ORDER BY id')
        await self.expect_preview_error('invoice_processing_receipt_owned', identifier, 'process_without_stock')
        await self.expect_preview_error('invoice_processing_receipt_owned', identifier, 'reopen')
        await self.change_receipt(session_id, 'cancelled', {'finalize_result': {'session_id': session_id},
            'receiving_archive': {'previous_status': 'completed', 'operation_id': str(uuid4())}})
        await self.perform(identifier, 'archive')
        archived = await self.current(identifier)
        self.assertEqual(archived['status'], 'unprocessed', 'Archived history no longer owns current document handling')
        self.assertTrue(archived['receipts'][0]['received'], 'The original receipt stock fact remains visible in history')
        self.assertTrue(archived['receipts'][0]['archived'])
        await self.perform(identifier, 'restore')
        restored = await self.perform(identifier, 'process_without_stock', note='Handle restored test invoice without a new receipt')
        self.assertEqual(restored['processing']['status'], 'processed_without_stock')
        self.assertTrue(restored['processing']['receipts'][0]['received'])
        self.assertEqual(await self.rows('SELECT * FROM stock_movements ORDER BY id'), original_movements)
        async with self.sessions() as db:
            with self.assertRaises(service.InvoiceProcessingError) as error:
                await service.assert_receiving_allowed(db, self.supplier_id, source['metadata']['invoice_number'], session_id)
            self.assertEqual(error.exception.code, 'invoice_processed_without_stock', 'Restore of the historical receipt must not bypass current handling')
        await self.perform(identifier, 'reopen')
        await self.change_receipt(session_id, 'cancelled', {'receiving_reversed': {'operation_id': str(uuid4())}, 'posting_revision': 1})
        processed = await self.perform(identifier, 'process_without_stock')
        self.assertEqual(processed['processing']['status'], 'processed_without_stock')
        self.assertEqual(await self.rows('SELECT * FROM stock_movements ORDER BY id'), original_movements)

    async def test_stale_preview_revalidates_receipt_facts_even_when_invoice_revision_did_not_change(self):
        identifier, source = await self.invoice()
        request = await self.reviewed(identifier, 'archive')
        session_id = await self.receipt(source, status='cancelled')
        with self.assertRaises(service.InvoiceProcessingError) as error:
            await self.apply(identifier, request)
        self.assertEqual(error.exception.code, 'invoice_processing_changed')
        self.assertEqual((await self.current(identifier))['revision'], 0)
        self.assertEqual((await self.current(identifier))['receipts'][0]['id'], session_id)
        self.assertEqual(await self.scalar('SELECT count(*) FROM invoice_processing_operations'), 0)

    async def test_exact_supplier_alias_guard_blocks_receiving_until_reopened_or_restored(self):
        alias = 'INDEX-ALIAS'
        identifier, source = await self.invoice(source_data={'index_candidates': [{'entry': {'number': alias}}]})
        await self.perform(identifier, 'process_without_stock')
        async with self.sessions() as db:
            for number in (source['source_key'], source['metadata']['invoice_number'], alias):
                with self.subTest(number=number), self.assertRaises(service.InvoiceProcessingError) as error:
                    await service.assert_receiving_allowed(db, self.supplier_id, number)
                self.assertEqual(error.exception.code, 'invoice_processed_without_stock')
            await service.assert_receiving_allowed(db, self.other_supplier_id, alias)
        await self.perform(identifier, 'reopen')
        await self.perform(identifier, 'archive')
        async with self.sessions() as db:
            with self.assertRaises(service.InvoiceProcessingError) as error:
                await service.assert_receiving_allowed(db, self.supplier_id, alias)
            self.assertEqual(error.exception.code, 'invoice_archived')
        await self.perform(identifier, 'restore')
        async with self.sessions() as db:
            await service.assert_receiving_allowed(db, self.supplier_id, alias)

    async def test_archive_blocks_new_settlements_and_metadata_but_preserves_existing_uuid_recovery(self):
        identifier, _ = await self.invoice(paid='0.00')
        payment = finance.PaymentRequest(request_id=uuid4(), expected_revision=0, kind='payment', amount='100.00',
                                         paid_at='2026-01-10T12:30:00Z', note='Verified synthetic payment')
        async with self.sessions() as db:
            paid = await finance.apply_payment(db, identifier, payment, actor='fixture-operator')
            await db.commit()
        await self.perform(identifier, 'archive')
        async with self.sessions() as db:
            self.assertEqual(await finance.apply_payment(db, identifier, payment, actor='fixture-operator'), paid)
        changed = payment.model_copy(update={'request_id': uuid4(), 'expected_revision': 1, 'kind': 'refund'})
        async with self.sessions() as db:
            with self.assertRaises(finance.FinanceError) as error:
                await finance.apply_payment(db, identifier, changed, actor='fixture-operator')
            self.assertEqual(error.exception.code, 'invoice_archived')
            with self.assertRaises(finance.FinanceError) as error:
                await finance.update_metadata(db, identifier, {'notes': 'Changed archived note'}, 1)
            self.assertEqual(error.exception.code, 'invoice_archived')
        await self.perform(identifier, 'restore')
        async with self.sessions() as db:
            refunded = await finance.apply_payment(db, identifier, changed, actor='fixture-operator')
            await db.commit()
        self.assertEqual((refunded['finance']['paid_amount'], refunded['finance']['status']), ('0.00', 'unpaid'))
        self.assertEqual(await self.scalar('SELECT count(*) FROM invoice_payments'), 3)
        await self.assert_no_inventory_effects()

    async def test_sale_cannot_be_manually_received_but_archive_and_restore_are_supported(self):
        identifier, _ = await self.invoice(direction='sale')
        await self.expect_preview_error('invoice_processing_purchase_required', identifier, 'process_without_stock')
        await self.expect_preview_error('invoice_processing_purchase_required', identifier, 'reopen')
        self.assertTrue((await self.perform(identifier, 'archive'))['processing']['archived'])
        self.assertFalse((await self.perform(identifier, 'restore'))['processing']['archived'])
        await self.assert_no_inventory_effects()

    async def test_active_invoice_list_stats_and_calendar_exclude_archive_until_requested(self):
        active, _ = await self.invoice(paid='0.00')
        retired, _ = await self.invoice(paid='0.00')
        await self.perform(retired, 'archive')
        app = FastAPI()
        app.include_router(routes.router)

        async def session():
            async with self.sessions() as db:
                yield db

        app.dependency_overrides[get_session] = session
        app.dependency_overrides[operator_access] = lambda: None
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://fixture.test') as client:
            for include_archived, expected in ((False, {active}), (True, {active, retired})):
                params = {'include_archived': str(include_archived).lower()}
                listed = await client.get('/invoices', params=params)
                self.assertEqual(listed.status_code, 200, listed.text)
                self.assertEqual({row['id'] for row in listed.json()['items']}, expected)
                stats = await client.get('/invoices/stats', params=params)
                self.assertEqual(stats.status_code, 200, stats.text)
                self.assertEqual(stats.json()['total_invoices'], len(expected))
                self.assertEqual(stats.json()['include_archived'], include_archived)
                suppliers = await client.get('/invoices/suppliers', params=params)
                self.assertEqual(suppliers.status_code, 200, suppliers.text)
                supplier = next(row for row in suppliers.json()['suppliers'] if row['id'] == self.supplier_id)
                self.assertEqual((supplier['invoice_count'], supplier['total_invoices']), (len(expected), len(expected)))
                async with self.sessions() as db:
                    calendar = await invoice_calendar.calendar(db, {'range_from': '2026-01-01', 'range_to': '2026-01-31',
                                                                    'include_archived': include_archived})
                self.assertEqual({row['id'] for row in calendar['items']}, expected)
            detail = await client.get(f'/invoices/{retired}')
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertTrue(detail.json()['processing']['archived'])
            self.assertEqual(detail.json()['processing']['allowed_actions'], ['restore'])

    async def test_legacy_index_uses_database_processing_and_archive_state_not_stale_file_flags(self):
        identifier, source = await self.invoice()
        number = source['metadata']['invoice_number']
        entries = [{'number': number, 'status': 'new'}, {'invoice_id': 'archive-fixture:MALFORMED-LOCAL', 'status': 'new'}]
        await self.perform(identifier, 'process_without_stock')
        async with self.sessions() as db:
            projected = await service.project_index(db, 'archive-fixture', entries)
        self.assertEqual((projected[0]['status'], projected[0]['processing_status'], projected[0]['archive_invoice_id']),
                         ('processed', 'processed_without_stock', identifier))
        await self.perform(identifier, 'archive')
        async with self.sessions() as db:
            await db.execute(text('''INSERT INTO invoice_source_archive_events(id,supplier_id,source_key,action,note,actor,request_hash)
                VALUES(:id,:supplier,'archive-fixture:MALFORMED-LOCAL','archive','Retire test source','fixture',:hash)'''),
                {'id': uuid4(), 'supplier': self.supplier_id, 'hash': 'a' * 64})
            await db.commit()
        async with self.sessions() as db:
            self.assertEqual(await service.project_index(db, 'archive-fixture', entries), [])
            all_entries = await service.project_index(db, 'archive-fixture', entries, include_archived=True)
        self.assertTrue(all_entries[0]['invoice_archived'])
        self.assertTrue(all_entries[1]['source_archived'])
        async with self.sessions() as db:
            different_source = await service.project_index(db, 'archive-fixture', [
                {'original_number': 'ACTIVE-LOCAL', 'number': 'archive-fixture:MALFORMED-LOCAL'}])
        self.assertEqual(len(different_source), 1, 'An alias of a retired source must not retire a distinct canonical source')
        self.assertFalse(different_source[0]['source_archived'])
        self.assertEqual(entries[0]['status'], 'new', 'Source index is not rewritten')
        await self.perform(identifier, 'restore')
        await self.perform(identifier, 'reopen')
        session_id = await self.receipt(source, status='cancelled', data={'receiving_archive': {'previous_status': 'new'}})
        async with self.sessions() as db:
            self.assertEqual(await service.project_index(db, 'archive-fixture', entries), [])
            projected = await service.project_index(db, 'archive-fixture', entries, include_archived=True)
        self.assertTrue(projected[0]['receiving_archived'])
        self.assertEqual(projected[0]['receiving_session_id'], str(session_id))

    async def test_audit_is_immutable_and_reapplying_schema_keeps_original_operations(self):
        identifier, _ = await self.invoice()
        result = await self.perform(identifier, 'process_without_stock')
        for sql in ('UPDATE invoice_processing_operations SET note=\'rewrite\'', 'DELETE FROM invoice_processing_operations'):
            async with self.sessions() as db:
                with self.assertRaises(DBAPIError) as error:
                    await db.execute(text(sql))
                self.assertIn('invoice_processing_immutable', str(error.exception))
                await db.rollback()
        import asyncpg
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'SET search_path TO "{self.schema}"')
            await connection.execute((Path(__file__).resolve().parents[2] / 'infra/db-init/051_invoice_processing.sql').read_text())
        finally:
            await connection.close()
        current = await self.current(identifier)
        self.assertEqual((current['revision'], current['status'], len(current['history'])), (1, 'processed_without_stock', 1))
        self.assertEqual(current['history'][0]['request_id'], result['request_id'])
