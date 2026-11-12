"""Synthetic ARIGA documents only. Private invoice/customer data never enters Git."""
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from inventory_hub import config_io
from inventory_hub.adapters import ariga_invoice as adapter
from inventory_hub.routers import receiving_db, suppliers
from inventory_hub.services import invoice_archive, receiving_source


def invoice_text():
    return """ARIGA s.r.o.
IČ 25130293, DIČ CZ25130293
Faktura číslo (variabilní symbol): 9000001
Datum vystavení: 01.02.2026   Datum uskutečnění zdanitelného plnění: 01.02.2026   Splatnost: 15.02.2026
Zboží                       Kód       Cena/Ks     Kusů    Celkem    DPH   Celkem
                                     bez DPH            bez DPH           s DPH
Synthetic light             DEMO-001   12,34 €      2     24,68 €    0%    24,68 €
Synthetic knife             DEMO-002    5,67 €      1      5,67 €    0%     5,67 €
Poštovné                               3,21 €             3,21 €    0%     3,21 €
Celkem                                                  33,56 €   0,00 € 33,56 €
"""


class ArigaParserTests(unittest.TestCase):
    def test_goods_net_postage_and_original_zero_vat(self):
        value = adapter.parse_invoice_text(invoice_text())
        self.assertEqual((value.number, value.issue_date.isoformat(), value.due_date.isoformat()),
                         ('9000001', '2026-02-01', '2026-02-15'))
        self.assertEqual((value.net, value.vat, value.gross), (Decimal('33.56'), Decimal('0'), Decimal('33.56')))
        self.assertEqual(value.postage.net, Decimal('3.21'))
        self.assertEqual([line.sku for line in value.lines], ['DEMO-001', 'DEMO-002'])
        self.assertEqual([line.price for line in value.lines], [Decimal('12.34'), Decimal('5.67')])
        self.assertTrue(all(line.vat == 0 and line.ean == '' for line in value.lines))
        self.assertNotIn('Poštovné', value.csv_bytes().decode())
        self.assertEqual(len(value.csv_bytes().decode().splitlines()), 3)
        self.assertEqual(adapter.parse_invoice_text(invoice_text().replace('DEMO-001', '00000001')).lines[0].sku, '00000001')

    def test_postage_optional_and_exact_source_tax_retained(self):
        value = invoice_text().replace('Poštovné                               3,21 €             3,21 €    0%     3,21 €\n', '')
        parsed = adapter.parse_invoice_text(value.replace('33,56', '30,35'))
        self.assertIsNone(parsed.postage)
        # Source invoice tax is independent of the Slovak catalog setting.
        taxed = value.replace('0%    24,68 €', '21%    29,86 €').replace('0%     5,67 €', '21%     6,86 €')
        taxed = taxed.replace('33,56 €   0,00 € 33,56 €', '30,35 €   6,37 € 36,72 €')
        self.assertEqual(adapter.parse_invoice_text(taxed).vat, Decimal('6.37'))

    def test_invalid_identity_currency_partial_table_and_totals_fail_closed(self):
        source = invoice_text()
        invalid = [source.replace('25130293', '00000000'), source.replace('ARIGA', 'OTHER'),
            source.replace('9000001', 'ABC'), source.replace('01.02.2026', '31.02.2026'),
            source.replace('€', 'Kč'), source.replace('Kusů', 'Unknown'),
            source.replace('DEMO-002', ''), source.replace('2     24,68', '0     24,68'),
            source.replace('12,34', '12,35'), source.replace('33,56 €   0,00', '33,57 €   0,00'),
            source.replace('Poštovné', 'Unknown charge'), source + '\nUnrecognized 1,00 €',
            source + source, source.replace(' 0% ', ' 23% ')]
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(adapter.InvoiceParseError):
                adapter.parse_invoice_text(text)

    def test_pdf_layout_and_unsupported_pages(self):
        page = SimpleNamespace(extract_text=Mock(return_value=invoice_text()))
        reader = SimpleNamespace(is_encrypted=False, pages=[page])
        with patch.object(adapter, 'PdfReader', return_value=reader):
            self.assertEqual(adapter.parse_invoice_pdf(b'%PDF-synthetic').number, '9000001')
            page.extract_text.assert_called_once_with(extraction_mode='layout')
            for pages, encrypted in (([page, page], False), ([], False), ([page], True)):
                reader.pages, reader.is_encrypted = pages, encrypted
                with self.assertRaises(adapter.InvoiceParseError):
                    adapter.parse_invoice_pdf(b'%PDF-synthetic')
            reader.pages, reader.is_encrypted = [page], False
            page.extract_text.return_value = ''
            with self.assertRaises(adapter.InvoiceParseError):
                adapter.parse_invoice_pdf(b'%PDF-synthetic')
        with self.assertRaises(adapter.InvoiceParseError):
            adapter.parse_invoice_pdf(b'not a pdf')


class ArigaWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(config_io, 'DATA_ROOT', self.root))
        self.enterContext(patch.object(suppliers, 'DATA_ROOT', self.root))
        self.enterContext(patch.object(invoice_archive.settings, 'INVENTORY_DATA_ROOT', self.root))
        self.archive = self.enterContext(patch.object(invoice_archive, 'archive_after_upload',
            AsyncMock(return_value={'status': 'completed'})))
        cfg = self.root / 'suppliers/ariga/config.json'
        cfg.parent.mkdir(parents=True)
        cfg.write_text('{"code":"ariga","name":"ARIGA","adapter_settings":{"vat_rate":23}}')
        self.invoice = adapter.parse_invoice_text(invoice_text())
        self.parser = self.enterContext(patch.object(adapter, 'parse_invoice_pdf', return_value=self.invoice))
        app = FastAPI()
        app.include_router(suppliers.router)
        self.client = self.enterContext(TestClient(app))

    def upload(self, **kwargs):
        return self.client.post('/suppliers/ariga/upload-invoice',
            files={'file': ('synthetic.pdf', b'%PDF-synthetic', 'application/pdf')}, **kwargs)

    def test_upload_replay_archive_and_verified_receiving_source(self):
        response = self.upload()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['items_count'], 2)
        self.assertFalse(result['replayed'])
        csv = self.root / result['csv_path']
        source = receiving_source.load_source(self.root, 'ariga', '9000001', csv, receiving_db._parse_invoice_csv)
        self.assertEqual(source.adapter, 'ariga_pdf')
        self.assertEqual(len(source.rows), 2)
        self.assertEqual(source.rows[0]['unit_price'], Decimal('12.34'))
        self.assertEqual(source.rows[0]['ordered_qty'], Decimal('2'))
        metadata, rows = invoice_archive.parse_uploaded_document('ariga', 'invoice.pdf', b'%PDF-synthetic')
        self.assertEqual(metadata['total_with_vat'], Decimal('33.56'))
        self.assertEqual(metadata['vat_amount'], Decimal('0'))
        self.assertEqual([row.supplier_sku for row in rows], ['DEMO-001', 'DEMO-002'])
        # Existing original/CSV archive path uses exactly the same source parser.
        index_path = self.root / 'suppliers/ariga/invoices/index.latest.json'
        index = json.loads(index_path.read_text())
        entry = index['ariga:9000001']
        archived, archived_rows, _ = invoice_archive._indexed_document(self.root, 'ariga', '9000001', entry)
        self.assertEqual(archived['total_with_vat'], metadata['total_with_vat'])
        self.assertEqual(len(archived_rows), 2)
        entry.update(status='processed', note='Preserve this')
        index_path.write_text(json.dumps(index))
        before = index_path.read_bytes()
        self.assertTrue(self.upload().json()['replayed'])
        self.assertEqual(index_path.read_bytes(), before)
        # Editing the intermediate cannot bypass verification of the PDF.
        csv.write_bytes(csv.read_bytes().replace(b'12.34', b'12.35'))
        with self.assertRaises(receiving_source.ReceivingSourceError) as raised:
            receiving_source.load_source(self.root, 'ariga', '9000001', csv, receiving_db._parse_invoice_csv)
        self.assertEqual(raised.exception.code, 'receiving_source_changed')

    def test_changed_invoice_and_postage_are_conflicts(self):
        self.assertEqual(self.upload().status_code, 200)
        value = invoice_text().replace('3,21', '4,21').replace('33,56', '34,56')
        self.parser.return_value = adapter.parse_invoice_text(value)
        self.assertNotEqual(self.invoice.fingerprint(), self.parser.return_value.fingerprint())
        self.assertEqual(self.upload().status_code, 409)

    def test_invalid_upload_cannot_create_receiving_source(self):
        self.assertEqual(self.upload(params={'invoice_number': 'WRONG'}).status_code, 422)
        self.parser.side_effect = adapter.InvoiceParseError('Unsupported document')
        self.assertEqual(self.upload().status_code, 422)
        self.assertFalse((self.root / 'suppliers/ariga/invoices').exists())
        self.assertEqual(invoice_archive.parse_uploaded_document('ariga', 'invoice.pdf', b'%PDF-synthetic'),
                         ({'parse_error': 'invoice_parse_failed'}, []))


class ArigaDuplicateSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_distinct_original_numbers_with_same_goods_are_not_duplicate_receipts(self):
        current = receiving_source.ReceivingSource([], 'new-source', 'same-csv', '9000002', 'ariga_pdf')
        proof = {**current.snapshot(), 'source_hash': 'old-source', 'document_number': '9000001'}
        existing = SimpleNamespace(id=1, source_hash='old-source', invoice_file_path=None,
            invoice_number='9000001', session_data={'receiving_source': proof}, status=SimpleNamespace(value='completed'))
        db = SimpleNamespace(scalars=AsyncMock(return_value=[existing]))
        await receiving_source.ensure_unique(db, 1, current, Path('/unused'))
        with self.assertRaises(receiving_source.ReceivingSourceError) as raised:
            await receiving_source.ensure_unique(db, 1, replace(current, document_number='9000001'), Path('/unused'))
        self.assertEqual(raised.exception.code, 'receiving_source_changed')
        with self.assertRaises(receiving_source.ReceivingSourceError) as raised:
            await receiving_source.ensure_unique(db, 1, replace(current, source_hash='old-source', document_number='9000001'), Path('/unused'))
        self.assertEqual(raised.exception.code, 'receiving_source_duplicate')


if __name__ == '__main__':
    unittest.main()
