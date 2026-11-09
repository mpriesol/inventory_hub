"""Malformed test sources can be retired and restored without deleting evidence."""
import json
import unittest
from uuid import uuid4
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from invoice_db_fixture import DATABASE_REQUIRED, TEST_URL, InvoiceDatabaseCase
from inventory_hub.invoice_archive_types import InvoiceReconcileInput
from inventory_hub.invoice_source_archive_types import SourceArchivePreview, SourceArchiveApply
from inventory_hub.services import invoice_archive as archive, invoice_source_archives as service


@unittest.skipUnless(TEST_URL, DATABASE_REQUIRED)
class InvoiceSourceArchiveDatabaseTests(InvoiceDatabaseCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        folder = self.data_root / 'suppliers/archive-fixture/invoices'
        folder.mkdir(parents=True)
        self.index = folder / 'index.latest.json'
        self.index.write_text(json.dumps({'invoices': [{'number': 'BROKEN-TEST'}]}))
        self.original_index = self.index.read_bytes()

    def payload(self, **changes):
        return SourceArchivePreview(**{'request_id': uuid4(), 'supplier_code': 'archive-fixture',
            'source_key': 'BROKEN-TEST', 'action': 'archive', 'note': 'Old test document; preserve original', **changes})

    async def prepare(self, payload):
        async with self.sessions() as db:
            return await service.preview(db, payload)

    async def apply(self, payload, preview):
        request = SourceArchiveApply(**payload.model_dump(), preview_hash=preview['preview_hash'], confirmed=True)
        async with self.sessions() as db:
            return await service.apply(db, request, 'test operator')

    async def reconcile(self):
        async with self.sessions() as db:
            result = await archive.reconcile_supplier(db, InvoiceReconcileInput(supplier_code='archive-fixture'))
            await db.commit()
            return result

    async def test_archive_restore_replays_and_originals_remain_unchanged(self):
        self.assertEqual(len((await self.reconcile())['conflicts']), 1)
        payload = self.payload()
        preview = await self.prepare(payload)
        self.assertTrue(preview['ready'])
        archived = await self.apply(payload, preview)
        self.assertFalse(archived['stock_effect'])
        self.assertEqual(await self.apply(payload, preview), archived)
        report = await self.reconcile()
        self.assertEqual((report['archived_skipped'], report['scanned'], report['conflicts']), (1, 0, []))
        restore = self.payload(action='restore', note='Restore for manual investigation')
        restored = await self.apply(restore, await self.prepare(restore))
        self.assertEqual(restored['action'], 'restore')
        self.assertEqual(len((await self.reconcile())['conflicts']), 1)
        # An old retry returns the original outcome; it cannot undo a later restore.
        self.assertEqual(await self.apply(payload, preview), archived)
        async with self.sessions() as db:
            status = await service.current(db, 'archive-fixture')
            self.assertFalse(status['items'][0]['archived'])
        self.assertEqual(await self.scalar('SELECT count(*) FROM invoice_source_archive_events'), 2)
        self.assertEqual(self.index.read_bytes(), self.original_index)
        await self.assert_no_inventory_effects()

    async def test_stale_preview_and_changed_uuid_payload_are_rejected(self):
        first, other = self.payload(), self.payload()
        first_preview, other_preview = await self.prepare(first), await self.prepare(other)
        await self.apply(first, first_preview)
        with self.assertRaisesRegex(archive.InvoiceArchiveError, 'preview_stale'):
            await self.apply(other, other_preview)
        with self.assertRaisesRegex(archive.InvoiceArchiveError, 'request_conflict'):
            await self.apply(first.model_copy(update={'note': 'different content'}), first_preview)
        with self.assertRaises(DBAPIError):
            async with self.sessions() as db:
                await db.execute(text("DELETE FROM invoice_source_archive_events"))
        self.assertEqual(await self.scalar('SELECT count(*) FROM invoice_source_archive_events'), 1)

    async def test_cannot_hide_an_active_invoice_via_source_retirement(self):
        async with self.sessions() as db:
            await archive.ingest_invoice(db, direction='purchase', source_kind='supplier',
                source_key='BROKEN-TEST', supplier_id=self.supplier_id,
                metadata={'invoice_number': 'BROKEN-TEST'}, lines=[],
                files=[{'filename': 'test.pdf', 'content': b'%PDF-1.4\nSynthetic test source\n%%EOF'}])
            await db.commit()
        prepared = await self.prepare(self.payload())
        self.assertFalse(prepared['ready'])
        self.assertIn('invoice_source_archive_invoice_first', prepared['blockers'])

    def test_blank_note_and_nonliteral_confirmation_are_invalid(self):
        with self.assertRaises(ValidationError):
            self.payload(note='   ')
        with self.assertRaises(ValidationError):
            SourceArchiveApply(**self.payload().model_dump(), preview_hash='a' * 64, confirmed=1)
