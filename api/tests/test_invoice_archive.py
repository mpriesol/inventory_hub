"""Pure archive parsing, exact amounts and private original-document handling."""
import csv
from decimal import Decimal
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from inventory_hub.invoice_archive_types import ArchiveLine, ArchiveMetadata
from inventory_hub.services import invoice_archive as service
from inventory_hub.file_access import stored_document_path


class InvoiceArchiveTests(unittest.TestCase):
    def test_source_hash_ignores_payment_observations_and_local_card_links(self):
        header = ArchiveMetadata(invoice_number='INV-1', currency='EUR', total_with_vat='12.30')
        row = ArchiveLine(line_number=1, quantity='1', unit_price='10', ean='5901234123457')
        later = header.model_copy(update={'source_paid_amount': Decimal('12.30'), 'source_payment_status': 'paid'})
        linked = row.model_copy(update={'matched_product_id': 12})
        self.assertEqual(service.source_hash(header, [row]), service.source_hash(later, [linked]))
        changed = header.model_copy(update={'total_with_vat': Decimal('12.31')})
        self.assertNotEqual(service.source_hash(header, [row]), service.source_hash(changed, [row]))

    def test_unknown_money_and_zero_tax_are_not_defaults(self):
        metadata = ArchiveMetadata()
        self.assertIsNone(metadata.invoice_number)
        self.assertIsNone(metadata.currency)
        self.assertIsNone(metadata.total_amount)
        self.assertEqual(ArchiveLine(line_number=1, quantity='1', unit_price='0', vat_rate='0').unit_price, Decimal(0))
        for value in ('NaN', 'Infinity', '1.001', 1.2, True):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ArchiveMetadata(total_amount=value)

    def test_compound_barcodes_keep_original_and_index_valid_gtin_only(self):
        row = ArchiveLine(line_number=1, quantity='1', ean='5901234123457;4006381333931;broken')
        values = service.barcode_values(row)
        self.assertIn('5901234123457', values)
        self.assertIn('05901234123457', values)
        self.assertNotIn('broken', values)

    def test_normalized_csv_preserves_quantity_cost_and_unknown_vat(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'invoice.csv'
            path.write_text('SCM;TITLE;QTY;PRICE;EAN\n00001;Source title;2,5;1,2500;5901234123457\n')
            rows = service._csv_lines(path)
            self.assertEqual((rows[0].supplier_sku, rows[0].quantity, rows[0].unit_price),
                             ('00001', Decimal('2.5'), Decimal('1.2500')))
            self.assertIsNone(rows[0].vat_rate)
            self.assertIsNone(rows[0].total_price_with_vat)
            path.write_text('SCM;QTY;PRICE\nX;invalid;1\n')
            with self.assertRaises((ValidationError, service.InvoiceArchiveError)):
                service._csv_lines(path)

    def test_whitespace_preamble_uses_actual_header_delimiter_and_mapped_source_values(self):
        mapping = {'SCM': 'SČM', 'EAN': 'EAN', 'TITLE': 'Názov',
                   'QTY': 'Počet kusov', 'UNIT_PRICE_EX': 'Jednotková cena'}
        for delimiter in (',', ';', '\t'):
            content = io.StringIO(' \r\n\t\r\n')
            content.seek(0, io.SEEK_END)
            writer = csv.writer(content, delimiter=delimiter)
            writer.writerow(['SČM', 'EAN', 'Názov', 'Počet kusov', 'Jednotková cena'])
            writer.writerow(['00001', '5901234123457', 'Synthetic, part', '2,5', '1,2500'])
            for encoding in ('utf-8-sig', 'cp1250'):
                with self.subTest(delimiter=delimiter, encoding=encoding):
                    rows = service._csv_content(content.getvalue().encode(encoding), mapping)
                    self.assertEqual(len(rows), 1)
                    self.assertEqual((rows[0].supplier_sku, rows[0].ean, rows[0].product_name),
                                     ('00001', '5901234123457', 'Synthetic, part'))
                    self.assertEqual((rows[0].quantity, rows[0].unit_price), (Decimal('2.5'), Decimal('1.2500')))
                    self.assertIsNone(rows[0].unit)
                    self.assertIsNone(rows[0].vat_rate)

    def test_configured_present_columns_stay_authoritative_and_invalid_data_is_not_skipped(self):
        header = ' \nQTY;Actual quantity;UNIT_PRICE_EX;Actual price\n'
        mapping = {'QTY': 'Actual quantity', 'UNIT_PRICE_EX': 'Actual price'}
        for values in ('2;;1.25;0.00\n', '2;invalid;1.25;0.00\n', '2;3;1.25;invalid\n'):
            with self.subTest(values=values), self.assertRaises(service.InvoiceArchiveError):
                service._csv_content((header + values).encode(), mapping)
        row = service._csv_content((header + '2;3;1.25;\n').encode(), mapping)[0]
        self.assertEqual(row.quantity, Decimal('3'))
        self.assertIsNone(row.unit_price, 'A blank mapped price must not fall back to another column')
        for content in (b'', b' \r\n\t\r\n', b' \nSCM;QTY\n'):
            with self.subTest(content=content), self.assertRaises(service.InvoiceArchiveError):
                service._csv_content(content)

    def test_canonical_ex_vat_price_is_preserved_without_deriving_missing_tax_or_gross(self):
        rows = service._csv_content(b'\nSCM;QTY;UNIT_PRICE_EX;UNIT_PRICE_INC\nA;1;0.0000;12.30\nB;2;;24.60\n')
        self.assertEqual(rows[0].unit_price, Decimal('0.0000'))
        self.assertIsNone(rows[1].unit_price)
        for row in rows:
            self.assertIsNone(row.vat_rate)
            self.assertIsNone(row.unit_price_with_vat)
            self.assertIsNone(row.total_price)
            self.assertIsNone(row.total_price_with_vat)
            self.assertIsNone(row.unit)

    def test_original_is_content_addressed_and_allowed_by_private_download_boundary(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            content = b'%PDF-1.4\nSynthetic original\n%%EOF'
            first = service._file(root, 'shops', 'test-shop', {'filename': 'faktura.pdf', 'content': content})
            again = service._file(root, 'shops', 'test-shop', {'filename': 'renamed.pdf', 'content': content})
            self.assertEqual(first['file_path'], again['file_path'])
            self.assertEqual(stored_document_path(root, first['file_path']).read_bytes(), content)
            with self.assertRaises(HTTPException):
                service._file(root, 'suppliers', 'test', {'path': '/etc/passwd'})

    def test_generic_upload_date_is_not_treated_as_invoice_date(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'suppliers/test/invoices/csv/INV.csv'
            path.parent.mkdir(parents=True)
            path.write_text('SCM;QTY;PRICE\nA;1;2\n')
            metadata, rows, files = service._indexed_document(root, 'test', 'INV', {
                'number': 'INV', 'issue_date': '2026-01-01', 'csv_path': 'invoices/csv/INV.csv'})
            self.assertIsNone(metadata['invoice_date'])
            self.assertIsNone(metadata['currency'])
            self.assertEqual(files[0]['role'], 'original')

    def test_duplicate_index_status_changes_keep_original_and_all_candidate_evidence(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            invoices = root / 'suppliers/test/invoices'
            (invoices / 'raw').mkdir(parents=True)
            (invoices / 'csv').mkdir()
            original = b'%PDF-1.4\nSynthetic original\n%%EOF'
            (invoices / 'raw/first.pdf').write_bytes(original)
            (invoices / 'raw/copy.pdf').write_bytes(original)
            csvs = [b'SCM;QTY;PRICE\nA;2;1.25\n', b'SCM;QTY;PRICE\r\nA;2;1.25\r\n']
            for name, content in zip(('first.csv', 'copy.csv'), csvs):
                (invoices / 'csv' / name).write_bytes(content)
            entries = [
                {'number': 'INV', 'status': 'new', 'downloaded_at': '2026-01-01',
                 'pdf_path': 'invoices/raw/first.pdf', 'csv_path': 'invoices/csv/first.csv'},
                {'number': 'INV', 'status': 'processed', 'note': 'Keep source note',
                 'downloaded_at': '2026-01-02', 'pdf_path': 'invoices/raw/copy.pdf', 'csv_path': 'invoices/csv/copy.csv'},
            ]
            header, rows, files, evidence = service._indexed_candidates(root, 'test', 'INV', entries)
            self.assertEqual((header.invoice_number, rows[0].quantity), ('INV', Decimal('2')))
            self.assertIsNone(header.currency)
            self.assertEqual([file['content'] for file in files if file['role'] == 'original'], [original])
            self.assertEqual({file['content'] for file in files if file['role'] == 'parsed'}, set(csvs))
            self.assertEqual([candidate['entry'] for candidate in evidence['index_candidates']], entries)
            self.assertTrue(all(len(candidate['files']) == 2 for candidate in evidence['index_candidates']))
            # Ingestion receives the bytes which were checked, not mutable cache paths.
            (invoices / 'raw/copy.pdf').write_bytes(b'replaced source cache')
            self.assertEqual(next(file['content'] for file in files if file['role'] == 'original'), original)

    def test_duplicate_index_candidates_reject_changed_totals_lines_or_original_bytes(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            invoices = root / 'suppliers/test/invoices'
            (invoices / 'raw').mkdir(parents=True)
            (invoices / 'csv').mkdir()
            (invoices / 'raw/original.pdf').write_bytes(b'%PDF-1.4\nOriginal\n%%EOF')
            (invoices / 'raw/different.pdf').write_bytes(b'%PDF-1.4\nDifferent\n%%EOF')
            (invoices / 'csv/original.csv').write_text('SCM;QTY;PRICE\nA;2;1.25\n')
            (invoices / 'csv/different.csv').write_text('SCM;QTY;PRICE\nA;3;1.25\n')
            entry = {'number': 'INV', 'total_with_vat': '3.08', 'currency': 'EUR',
                     'pdf_path': 'invoices/raw/original.pdf', 'csv_path': 'invoices/csv/original.csv'}
            for changed in ({'total_with_vat': '3.09'}, {'currency': 'CZK'},
                            {'total_with_vat': None}, {'csv_path': 'invoices/csv/different.csv'},
                            {'pdf_path': 'invoices/raw/different.pdf'}):
                with self.subTest(changed=changed), self.assertRaises(service.InvoiceArchiveError) as raised:
                    service._indexed_candidates(root, 'test', 'INV', [entry, {**entry, **changed}])
                self.assertEqual(raised.exception.code, 'invoice_index_ambiguous')

    def test_manual_csv_upload_indexes_exact_barcodes_and_preserves_unknown_totals(self):
        with TemporaryDirectory() as folder, patch.object(service.settings, 'INVENTORY_DATA_ROOT', folder):
            metadata, rows = service.parse_uploaded_document('test', 'source.csv',
                b'SCM;QTY;PRICE;EAN\n0001;2;1.25;5901234123457\n')
            self.assertEqual(metadata, {})
            self.assertEqual((rows[0].supplier_sku, rows[0].quantity, rows[0].ean),
                             ('0001', Decimal('2'), '5901234123457'))
            self.assertIsNone(rows[0].vat_rate)

    def test_manual_supported_pdf_upload_reuses_validated_source_parser(self):
        from inventory_hub.adapters import sk_profi_bike_invoice
        parsed = SimpleNamespace(number='2026/1', issue_date=None, due_date=None,
            gross=Decimal('12.30'), net=Decimal('10.00'), vat=Decimal('2.30'), lines=[SimpleNamespace(
                number=1, sku='0001', title='Original title', qty=Decimal('1'), price=Decimal('10'),
                ean='5901234123457', vat=Decimal('23'), net=Decimal('10'), gross=Decimal('12.30'))])
        original = b'%PDF-1.4\nSynthetic original\n%%EOF'
        with patch.object(sk_profi_bike_invoice, 'parse_invoice_pdf', return_value=parsed) as parser:
            metadata, rows = service.parse_uploaded_document('sk-profi-bike', 'invoice.pdf', original)
            parser.assert_called_once_with(original)
        self.assertEqual((metadata['invoice_number'], metadata['total_with_vat']), ('2026/1', Decimal('12.30')))
        self.assertEqual(rows[0].ean, '5901234123457')

    def test_manual_unsupported_or_invalid_document_keeps_original_without_invented_lines(self):
        self.assertEqual(service.parse_uploaded_document('other', 'invoice.pdf', b'%PDF-1.4\n%%EOF'), ({}, []))
        with TemporaryDirectory() as folder, patch.object(service.settings, 'INVENTORY_DATA_ROOT', folder):
            self.assertEqual(service.parse_uploaded_document('test', 'invalid.csv', b'QTY;PRICE\nwrong;1\n'),
                             ({'parse_error': 'invoice_parse_failed'}, []))


if __name__ == '__main__':
    unittest.main()
