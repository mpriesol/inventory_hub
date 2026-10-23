"""Archive mirroring: exact source facts, bounded continuation and replay."""
from copy import deepcopy
from datetime import date
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from inventory_hub.invoice_archive_types import ArchiveMetadata, ArchiveLine
from inventory_hub.services import invoice_archive, shop_invoice_sync as service
from inventory_hub.shop_invoice_types import ShopInvoiceSyncRequest


def document(number='INV-1', **changes):
    return {'invoice_number': number, 'type': 'invoice', 'currency_id': 'CZK',
        'date_of_issuance': '2026-10-22', 'date_of_expiration': '2026-11-05',
        'total_with_vat': '123.00', 'total_without_vat': '100.00', 'total_rest': '23.00',
        'paid_yn': False, 'paid_date': None,
        'supplier': {'name': 'Original shop seller'}, 'customer': {'name': 'Original customer'},
        'invoice_pdf_url': 'http://169.254.169.254/never-follow',
        'items': [{'code': 'REMOTE-SKU', 'name': 'Original service', 'type': 'product', 'unit': 'hour', 'quantity': '2',
            'price_per_unit_without_vat': '50.0000', 'price_without_vat': '100.00', 'vat': '23',
            'price_per_unit_with_vat': '61.5000', 'price_with_vat': '123.00'}], **changes}


class ShopInvoiceFactsTests(unittest.TestCase):
    def test_original_currency_prices_units_and_parties_are_preserved_without_product_guess(self):
        original = document()
        frozen = deepcopy(original)
        header, lines, fingerprint = service.normalize(original)
        self.assertEqual(original, frozen)
        self.assertEqual((header['currency'], header['total_amount'], header['vat_amount']), ('CZK', '123.00', '23.00'))
        self.assertEqual((header['source_paid_amount'], header['source_payment_status']), ('100.00', 'not_paid'))
        self.assertEqual((lines[0]['supplier_sku'], lines[0]['unit'], lines[0]['unit_price']), ('REMOTE-SKU', 'hour', '50.0000'))
        self.assertIsNone(lines[0]['matched_product_id'])
        self.assertIsNone(lines[0]['ean'])
        self.assertEqual(len(fingerprint), 64)
        ArchiveMetadata.model_validate(header)
        ArchiveLine.model_validate(lines[0])

    def test_credit_note_amounts_stay_signed_and_original_reference_changes_fingerprint(self):
        raw = document(type='creditNote', total_with_vat='-123.00', total_without_vat='-100.00', total_rest='-23.00',
                       related_invoice_number='ORIGINAL-1')
        header, _, fingerprint = service.normalize(raw)
        self.assertEqual((header['document_type'], header['total_amount'], header['vat_amount'], header['source_paid_amount']),
                         ('credit_note', '-123.00', '-23.00', '-100.00'))
        self.assertNotEqual(fingerprint, service.normalize({**raw, 'related_invoice_number': 'ORIGINAL-2'})[2])

    def test_source_payment_refresh_does_not_rewrite_immutable_invoice_hash(self):
        raw = document()
        updated = {**raw, 'paid_yn': True, 'paid_date': '2026-10-23', 'total_rest': '0.00',
                   'last_update_time': '2026-10-23T08:00:00+02:00', 'invoice_pdf_url': 'https://new.invalid/original.pdf'}
        header, _, fingerprint = service.normalize(updated)
        self.assertEqual(fingerprint, service.normalize(raw)[2])
        self.assertEqual(header['source_paid_amount'], '123.00')
        self.assertEqual(header['source_paid_date'], '2026-10-23')
        self.assertIsNone(header['source_paid_at'], 'A date-only provider field must not invent an exact timestamp')
        changed_customer = {**raw, 'customer': {'name': 'Different buyer'}}
        self.assertNotEqual(fingerprint, service.normalize(changed_customer)[2])

    def test_partial_or_unknown_payment_is_not_assumed_zero_and_overpayment_is_not_clamped(self):
        raw = document()
        raw.pop('total_rest')
        header, _, _ = service.normalize(raw)
        self.assertEqual(header['source_payment_status'], 'not_paid')
        self.assertIsNone(header['source_paid_amount'])
        self.assertEqual(service.normalize({**raw, 'paid_yn': True})[0]['source_paid_amount'], '123.00')
        self.assertEqual(service.normalize({**raw, 'total_rest': '-1.00'})[0]['source_paid_amount'], '124.00')

    def test_unknown_currency_type_or_nonfinite_amount_is_visible(self):
        for changes, code in (({'currency_id': None}, 'invoice_source_currency_invalid'),
                              ({'type': 'proforma'}, 'invoice_source_type_unsupported'),
                              ({'total_with_vat': 'NaN'}, 'invoice_source_amount_invalid')):
            with self.subTest(code=code), self.assertRaisesRegex(service.source.SourceError, code):
                service.normalize(document(**changes))

    def test_continuation_requires_bounded_dates_and_verified_page(self):
        with self.assertRaises(ValidationError):
            ShopInvoiceSyncRequest(date_from='2020-01-01', date_to='2026-01-01')
        with self.assertRaises(ValidationError):
            ShopInvoiceSyncRequest(date_from='2026-01-01', date_to='2026-01-31', offset=10)


class ShopInvoiceSyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = SimpleNamespace(scalar=AsyncMock(return_value=SimpleNamespace(id=7, platform='upgates')),
            commit=AsyncMock(), rollback=AsyncMock(), execute=AsyncMock(), scalars=AsyncMock(return_value=[]))
        self.request = ShopInvoiceSyncRequest(date_from='2026-10-01', date_to='2026-10-31')
        self.page = {'invoices': [document()], 'fingerprint': 'f' * 64, 'page_token': 'a' * 64,
                     'page': 1, 'pages': 1, 'total': 1}
        self.pdf = b'%PDF-1.7\noriginal source bytes\n%%EOF'
        self.patches = [patch.object(service.source, 'read_page', AsyncMock(return_value=self.page)),
                       patch.object(service.source, 'read_pdf', AsyncMock(return_value=self.pdf)),
                       patch.object(invoice_archive, 'ingest_invoice', AsyncMock(return_value={'id': 12, 'replayed': False}))]
        self.read, self.download, self.ingest = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)

    async def test_source_identity_original_bytes_and_full_facts_reach_common_ingest(self):
        result = await service.refresh(self.db, 'biketrek', self.request, actor='operator-1')
        self.assertEqual((result['imported'], result['unchanged'], result['failed'], result['next']), (1, 0, 0, None))
        self.download.assert_awaited_once_with('biketrek', 'INV-1', 'f' * 64)
        self.assertEqual(self.db.commit.await_count, 2)
        kwargs = self.ingest.await_args.kwargs
        self.assertEqual((kwargs['direction'], kwargs['source_kind'], kwargs['source_key'], kwargs['shop_id']),
                         ('sale', 'shop', 'INV-1', 7))
        self.assertEqual(kwargs['files'][0]['content'], self.pdf)
        self.assertEqual(kwargs['source_data'], self.page['invoices'][0])
        self.assertEqual(kwargs['actor'], 'operator-1')

    async def test_repeat_uses_existing_archive_result(self):
        self.ingest.return_value = {'id': 12, 'replayed': True}
        result = await service.refresh(self.db, 'biketrek', self.request)
        self.assertEqual((result['imported'], result['unchanged'], result['items'][0]['id']), (0, 1, 12))

    async def test_ten_document_limit_has_exact_page_continuation(self):
        self.page.update(invoices=[document(f'INV-{i}') for i in range(12)], total=12)
        result = await service.refresh(self.db, 'biketrek', self.request)
        self.assertEqual(self.download.await_count, 10)
        self.assertEqual(result['next'], {'page': 1, 'offset': 10, 'page_token': 'a' * 64})
        self.assertEqual(result['remote_requests'], 11)

    async def test_changed_page_is_rejected_before_any_document_download_or_archive_write(self):
        request = self.request.model_copy(update={'offset': 1, 'page_token': 'b' * 64})
        with self.assertRaisesRegex(service.source.SourceError, 'invoice_source_page_changed'):
            await service.refresh(self.db, 'biketrek', request)
        self.download.assert_not_awaited()
        self.ingest.assert_not_awaited()

    async def test_rate_limit_keeps_committed_document_and_resumes_at_unsent_document(self):
        self.page.update(invoices=[document('A'), document('B')], total=2)
        self.download.side_effect = [self.pdf, service.source.SourceError(
            'invoice_source_rate_limited', 429, 55, upstream_status=429, operation='pdf')]
        result = await service.refresh(self.db, 'biketrek', self.request)
        self.assertEqual((result['imported'], result['retry_after'], result['stopped_error']), (1, 55, 'invoice_source_rate_limited'))
        self.assertEqual(result['next'], {'page': 1, 'offset': 1, 'page_token': 'a' * 64})
        self.assertEqual((result['upstream_status'], result['operation']), (429, 'pdf'))
        self.assertEqual(self.ingest.await_count, 1)
        self.assertEqual(self.db.commit.await_count, 2)

    async def test_changed_archived_facts_are_reported_without_overwrite_or_stopping_other_documents(self):
        self.page.update(invoices=[document('A'), document('B')], total=2)
        self.ingest.side_effect = [invoice_archive.InvoiceArchiveError('invoice_source_changed'), {'id': 13, 'replayed': False}]
        result = await service.refresh(self.db, 'biketrek', self.request)
        self.assertEqual((result['failed'], result['imported']), (1, 1))
        self.assertEqual(result['items'][0]['error'], 'invoice_source_changed')
        self.db.rollback.assert_awaited_once()

    async def test_sale_lines_bind_only_one_exact_native_verified_shop_leaf(self):
        mapping = SimpleNamespace(id=9, product_id=42, shop_id=7, external_code='REMOTE-SKU',
            external_product_id='80', external_variant_id='81', is_variant=True, is_listed=True)
        self.db.scalars.return_value = [mapping]
        await service.refresh(self.db, 'biketrek', self.request)
        line = self.ingest.await_args.kwargs['lines'][0]
        self.assertEqual(line['matched_product_id'], 42)
        self.assertEqual(line['match_evidence']['external_variant_id'], '81')
        self.assertEqual(line['match_evidence']['shop_id'], 7)
        ArchiveLine.model_validate(line)
        for mappings in ([mapping, SimpleNamespace(**{**vars(mapping), 'id': 10, 'product_id': 43})],
                         [SimpleNamespace(**{**vars(mapping), 'external_product_id': None})],
                         [SimpleNamespace(**{**vars(mapping), 'external_code': 'remote-sku'})]):
            with self.subTest(mappings=mappings):
                self.db.scalars.return_value = mappings
                await service.refresh(self.db, 'biketrek', self.request)
                self.assertIsNone(self.ingest.await_args.kwargs['lines'][0]['matched_product_id'])

    async def test_non_product_charge_never_binds_to_card_with_coincidentally_equal_code(self):
        mapping = SimpleNamespace(id=9, product_id=42, shop_id=7, external_code='REMOTE-SKU',
            external_product_id='80', external_variant_id=None, is_variant=False, is_listed=True)
        self.db.scalars.return_value = [mapping]
        product_row = document()['items'][0]
        for kind in ('shipping', 'payment', 'discount', None):
            with self.subTest(kind=kind):
                self.page['invoices'] = [document(items=[{**product_row, 'type': kind}])]
                await service.refresh(self.db, 'biketrek', self.request)
                line = self.ingest.await_args.kwargs['lines'][0]
                self.assertEqual(line['supplier_sku'], 'REMOTE-SKU')
                self.assertIsNone(line['matched_product_id'])

    async def test_non_upgates_shop_never_calls_provider(self):
        self.db.scalar.return_value.platform = 'atomer'
        with self.assertRaisesRegex(service.source.SourceError, 'invoice_sync_platform_unsupported'):
            await service.refresh(self.db, 'xtrek', self.request)
        self.read.assert_not_awaited()
        self.download.assert_not_awaited()
        self.ingest.assert_not_awaited()
