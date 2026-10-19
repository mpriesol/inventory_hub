"""Synthetic Zookee exports and invoices; no private feed URLs or customer data."""
import io
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from pypdf import PdfWriter

from inventory_hub.adapters import zookee_catalog as catalog, zookee_invoice as invoice
from inventory_hub.adapters.sk_profi_bike_invoice import InvoiceParseError, InvoiceConflictError, save_receiving_invoice
from inventory_hub.services.supplier_availability_source import parse
from inventory_hub.config_normalize import normalize_supplier_config
from inventory_hub.services.supplier_availability_publication import desired_label


def isdoc():
    return b'''<Invoice xmlns="http://isdoc.cz/namespace/2013"><DocumentType>1</DocumentType><ID>9990001</ID>
    <AccountingSupplierParty><Party><PartyIdentification><ID>26001217</ID></PartyIdentification></Party></AccountingSupplierParty>
    <IssueDate>2026-01-01</IssueDate><LocalCurrencyCode>CZK</LocalCurrencyCode><ForeignCurrencyCode>EUR</ForeignCurrencyCode>
    <InvoiceLines><InvoiceLine><ID>1</ID><InvoicedQuantity unitCode="ks">3</InvoicedQuantity>
    <LineExtensionAmountCurr>30.00</LineExtensionAmountCurr><LineExtensionAmount>750.00</LineExtensionAmount>
    <LineExtensionAmountTaxInclusiveCurr>30.00</LineExtensionAmountTaxInclusiveCurr><UnitPrice>250.00</UnitPrice>
    <ClassifiedTaxCategory><Percent>0</Percent></ClassifiedTaxCategory>
    <Item><Description>Synthetic product</Description><SellersItemIdentification><ID>DEMO-1</ID></SellersItemIdentification>
    <CatalogueItemIdentification><ID>5901234123457</ID></CatalogueItemIdentification></Item></InvoiceLine></InvoiceLines>
    <LegalMonetaryTotal><TaxExclusiveAmountCurr>30.00</TaxExclusiveAmountCurr><TaxInclusiveAmountCurr>30.00</TaxInclusiveAmountCurr></LegalMonetaryTotal>
    <PaymentMeans><Payment><Details><PaymentDueDate>2026-02-01</PaymentDueDate></Details></Payment></PaymentMeans></Invoice>'''


def pdf(xml):
    writer = PdfWriter(); writer.add_blank_page(width=500, height=700); writer.add_attachment('invoice.isdoc', xml)
    result = io.BytesIO(); writer.write(result); return result.getvalue()


class ZookeeInvoiceTests(unittest.TestCase):
    def test_foreign_currency_line_totals_not_local_unit_price(self):
        parsed = invoice.parse_invoice_pdf(pdf(isdoc()))
        self.assertEqual(parsed.net, D('30'))
        self.assertEqual(parsed.lines[0].price, D('10.0000'))
        self.assertIn(b'DEMO-1;Synthetic product;3;10.0000;5901234123457', parsed.csv_bytes())

    def test_bad_currency_supplier_totals_identity_and_dtd_rejected(self):
        for old, new in [(b'>EUR<', b'>CZK<'), (b'>26001217<', b'>00000001<'),
                         (b'<TaxExclusiveAmountCurr>30.00', b'<TaxExclusiveAmountCurr>31.00'),
                         (b'>DEMO-1<', b'>../escape<'), (b'>5901234123457<', b'>not-a-barcode<'),
                         (b'<DocumentType>1', b'<DocumentType>2'), (b'<InvoicedQuantity unitCode="ks">3', b'<InvoicedQuantity unitCode="ks">0')]:
            with self.subTest(old=old), self.assertRaises(InvoiceParseError):
                invoice.parse_isdoc(isdoc().replace(old, new))
        with self.assertRaises(InvoiceParseError): invoice.parse_isdoc(b'<!DOCTYPE Invoice [<!ENTITY a "test">]>' + isdoc())

    def test_missing_attachment_and_duplicate_invoice_are_safe(self):
        writer = PdfWriter(); writer.add_blank_page(width=500, height=700); out = io.BytesIO(); writer.write(out)
        with self.assertRaises(InvoiceParseError): invoice.parse_invoice_pdf(out.getvalue())
        with tempfile.TemporaryDirectory() as folder:
            args = (pdf(isdoc()), Path(folder), 'zookee')
            first = save_receiving_invoice(*args, parser=invoice.parse_invoice_pdf)
            again = save_receiving_invoice(*args, parser=invoice.parse_invoice_pdf)
            self.assertFalse(first['replayed']); self.assertTrue(again['replayed'])
            with self.assertRaises(InvoiceConflictError):
                save_receiving_invoice(pdf(isdoc().replace(b'Synthetic product', b'Different product')), Path(folder), 'zookee', parser=invoice.parse_invoice_pdf)


ITEM = '''<ITEM><ID>1</ID><STAGE>draft</STAGE><kod>DEMO-1</kod><TITLE>Synthetic shirt S</TITLE><BRAND_ID>SENSOR</BRAND_ID>
<BARCODE>5901234123457</BARCODE><ITEMGROUP_ID>group-1</ITEMGROUP_ID><ITEMGROUP_TITLE>Synthetic shirt</ITEMGROUP_TITLE>
<PARAM><NAME>SIZE</NAME><VALUE>S</VALUE></PARAM><VARIABLE_PARAMS><PARAM>SIZE</PARAM></VARIABLE_PARAMS>
<VAT>21</VAT><RRP>121</RRP><PRICE>108.9</PRICE><sklad>2</sklad><LONGDESC>Line one\nLine two</LONGDESC></ITEM>'''
CFG = {'adapter_settings': {'mapping': {'postprocess': {'product_code_prefix': 'ZOO-'}}},
       'feeds': {'sources': {'stock': {'type': 'stock'}}}}


class ZookeeCatalogTests(unittest.TestCase):
    def parse(self, xml):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'export.xml'; path.write_text(xml)
            return catalog.parse_catalog(path, 'zookee', CFG)

    def test_explicit_variants_source_prices_and_draft_stock(self):
        second = ITEM.replace('DEMO-1', 'DEMO-2').replace('<VALUE>S</VALUE>', '<VALUE>M</VALUE>').replace('5901234123457', '4006381333931')
        rows = self.parse('<ITEMS>' + ITEM + second + '</ITEMS>')
        first = rows[0][0]
        self.assertEqual(first.shop_code, 'ZOO-DEMO-1'); self.assertEqual(first.group_code, 'group-1')
        self.assertEqual(first.variant_attributes[0].value, 'S')
        self.assertTrue(first.supplier_available)
        self.assertEqual(first.prices.currency, 'CZK'); self.assertIsNone(first.prices.purchase_net)
        self.assertEqual(first.prices.retail_net, D('100')); self.assertEqual(first.prices.discount_gross, D('108.9'))

    def test_uncoded_rows_are_visible_and_no_identity_is_invented(self):
        rows = self.parse('<ITEMS>' + ITEM + ITEM.replace('<kod>DEMO-1</kod>', '<kod/>') + '</ITEMS>')
        self.assertEqual(len(rows), 1); self.assertIn('source_rows_without_code:1', rows[0][0].warnings)
        with self.assertRaises(ValueError): self.parse('<ITEMS>' + ITEM + ITEM + '</ITEMS>')

    def test_stock_only_rows_extend_identity_without_overwriting_rich_content(self):
        stock = '<export><item><code>DEMO-1</code><title>Basic title</title></item><item><code>OLDER-1</code><title>Older item</title><brand>TOPEAK</brand><ean>4006381333931</ean></item></export>'
        with tempfile.TemporaryDirectory() as folder:
            rich = Path(folder) / 'rich.xml'; rich.write_text('<ITEMS>' + ITEM + '</ITEMS>')
            basic = Path(folder) / 'stock.xml'; basic.write_text(stock)
            rows = catalog.parse_catalog(rich, 'zookee', CFG, stock_path=basic)
        self.assertEqual(len(rows),2); self.assertEqual(rows[0][0].name,'Synthetic shirt S')
        self.assertIn('stock_feed_only',rows[1][0].warnings); self.assertEqual(rows[1][0].eans,['4006381333931'])
        self.assertIsNone(rows[1][0].group_code); self.assertIsNone(rows[1][0].prices.purchase_net)

    def test_stock_unknown_invalid_and_zero_are_separate(self):
        xml = '<export>' + ''.join(f'<item><code>{code}</code><inStock>{qty}</inStock></item>' for code, qty in [('A','0'),('B','2'),('C',''),('D','bad'),('','2')]) + '</export>'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'stock.xml'; path.write_text(xml)
            result = parse(path, 'zookee', CFG, 'stock', 'zookee')
            self.assertEqual([(r.sku,r.available) for r in result], [('A',False),('B',True)])
            self.assertEqual(result.report()['unknown'],1); self.assertEqual(result.report()['rejected'],2)

    def test_dtd_and_wrong_export_rejected(self):
        for xml in ['<export><item/></export>', '<!DOCTYPE ITEMS [<!ENTITY x "test">]><ITEMS>' + ITEM + '</ITEMS>']:
            with self.assertRaises(ValueError): self.parse(xml)

    def test_scoped_publication_policy_and_stock_priority(self):
        cfg = normalize_supplier_config({'adapter_settings': {'availability_publication': {'enabled': True, 'shop_code':'biketrek','brands':['Topeak','sensor']}}})
        self.assertEqual(cfg['adapter_settings']['availability_publication']['brands'], ['SENSOR','TOPEAK'])
        for policy in [{}, {'enabled':True,'shop_code':'biketrek','brands':[]}, {'enabled':'true','shop_code':'biketrek','brands':['SENSOR']}]:
            with self.assertRaises(ValueError): normalize_supplier_config({'adapter_settings':{'availability_publication':policy}})
        labels = {'orderable':'do 5 dní','unavailable':'vypredané'}
        self.assertEqual(desired_label('2', False, labels), 'SKLADOM')
        self.assertEqual(desired_label('0', True, labels), 'do 5 dní')
        self.assertEqual(desired_label('0', False, labels), 'vypredané')
        self.assertIsNone(desired_label('0', None, labels))
