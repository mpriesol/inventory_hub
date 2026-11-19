"""Synthetic Northfinder layouts. Customer documents stay outside Git."""
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from inventory_hub.adapters import northfinder_invoice as adapter
from inventory_hub.adapters.northfinder_web import NorthfinderConfig, NorthfinderInvoiceDownloader
from inventory_hub.adapters.sk_profi_bike_invoice import save_receiving_invoice


ISSUER = '''NORTHFINDER  a.s.
IČO / Reg.No.: 35933011
Dátum vystavenia / Issuing:  01.02.2026
Dátum splatnosti / Maturity:  15.02.2026'''
HEADER = 'Kód  Popis produktu  Množstvo  MJ  Cena  Zľava  Cena po zľave  Sadzba  Základ  Celkom s DPH'
EN_HEADER = 'Code  Product description  Quantity  CU  Price  Discou  Final Price  VAT Rat  Total NET  Total with VAT'
FIRST = '990001-101-102  Synthetic product Red M  2    20,00  10,0%  18,00  23%  36,00  44,28 EUR'
SECOND = '990002-101-103  Synthetic product Red L  1 ks  24,00  0,0%  24,00  23%  24,00  29,52 EUR'
CHARGES = '''ART00784  Doprava  1  5,00  0,0%  5,00  23%  5,00  6,15 EUR
ART00930  Zľava - Discount  1  - 7,01  0,0%  - 7,01  23%  - 7,01  - 8,62 EUR'''


def _page(rows, current, count, *, totals=None):
    parts = ['FAKTÚRA VYSTAVENÁ č. - INVOICE no.:  9900010']
    if current == 1:
        parts.append(ISSUER)
    parts.extend((HEADER, EN_HEADER, rows))
    if totals:
        net, vat, gross = totals
        parts.extend(('Celkom / Total:  3', 'Zaúčtovanie / Posting',
                      f'CELKOM / TOTAL  {net}  {vat}  {gross}', f'Celková čiastka / Total amount  {gross} EUR'))
    parts.extend(('Process by  Money S5', f'Vytlačil(a): fixture  {current}/{count}  Vystavil: fixture'))
    return '\n'.join(parts)


def invoice_pages():
    return [_page(FIRST, 1, 2), _page(SECOND + '\n' + CHARGES, 2, 2, totals=('57,99', '13,34', '71,33'))]


class NorthfinderPdfTests(unittest.TestCase):
    def test_single_page_uses_final_unit_price_and_exact_quantities(self):
        parsed = adapter.parse_invoice_pages([_page(FIRST + '\n' + SECOND, 1, 1, totals=('60,00', '13,80', '73,80'))])
        self.assertEqual((parsed.number, parsed.issue_date.isoformat(), parsed.due_date.isoformat()),
                         ('9900010', '2026-02-01', '2026-02-15'))
        self.assertEqual([(row.sku, row.price, row.qty, row.ean) for row in parsed.receiving_lines],
                         [('990001-101-102', D('18.00'), D(2), ''), ('990002-101-103', D('24.00'), D(1), '')])

    def test_multipage_invoice_keeps_charges_and_vat_but_receives_only_printed_goods_cost(self):
        pages = [SimpleNamespace(extract_text=Mock(return_value=value)) for value in invoice_pages()]
        with patch.object(adapter, 'PdfReader', return_value=SimpleNamespace(is_encrypted=False, pages=pages)):
            parsed = adapter.parse_invoice_pdf(b'%PDF-synthetic')
        self.assertEqual([row.net for row in parsed.lines], [D('36.00'), D('24.00'), D('5.00'), D('-7.01')])
        self.assertEqual((parsed.net, parsed.vat, parsed.gross), (D('57.99'), D('13.34'), D('71.33')))
        self.assertEqual([row.price for row in parsed.receiving_lines], [D('18.00'), D('24.00')])
        self.assertEqual(len(parsed.csv_bytes().splitlines()), 3)
        self.assertNotIn(b'ART0', parsed.csv_bytes())
        for page in pages:
            page.extract_text.assert_called_once_with(extraction_mode='layout')

    def test_invoice_rounding_by_tax_rate_is_supported(self):
        goods = '\n'.join(f'990001-101-{i}  Fixture  1  0,02  0%  0,02  23%  0,02  0,02 EUR' for i in range(2))
        parsed = adapter.parse_invoice_pages([_page(goods, 1, 1, totals=('0,04', '0,01', '0,05'))])
        self.assertEqual((parsed.gross, sum(row.gross for row in parsed.lines)), (D('0.05'), D('0.04')))

    def test_rejects_missing_wrong_and_inconsistent_pages(self):
        first, last = invoice_pages()
        bad = ([first], [last, first], [first, last.replace('9900010', '9900011')],
               [first, last.replace(SECOND, '')], [first.replace('35933011', '00000000'), last],
               [first, last.replace('ART00784', 'ART12345')], [first, last.replace(' EUR', ' CZK')],
               [first.replace('18,00', '18,01'), last])
        for index, pages in enumerate(bad):
            with self.subTest(case=index), self.assertRaises(adapter.InvoiceParseError):
                adapter.parse_invoice_pages(pages)

    def test_existing_pdf_receiving_source_survives_b2b_refresh(self):
        invoice = adapter.parse_invoice_pages(invoice_pages())
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved = save_receiving_invoice(b'%PDF-synthetic', root, 'northfinder', parser=lambda _: invoice)
            downloader = NorthfinderInvoiceDownloader(NorthfinderConfig(), root, 'northfinder')
            download_record = {'number': invoice.number, 'row_id': '990001'}
            before_index = downloader.index_path.read_bytes()
            with (patch.object(downloader, '_fetch_invoice_list', return_value=[download_record]),
                  patch.object(downloader, '_parse_invoice_record', return_value=download_record),
                  patch.object(downloader, '_fetch_invoice_detail') as download):
                outcome = downloader.refresh()
                download.assert_not_called()
            self.assertEqual((outcome.downloaded, outcome.skipped, outcome.errors), (0, 1, []))
            self.assertEqual(downloader.index_path.read_bytes(), before_index)
            self.assertEqual((root / saved['csv_path']).read_bytes(), invoice.csv_bytes())
