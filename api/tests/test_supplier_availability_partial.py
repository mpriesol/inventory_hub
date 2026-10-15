"""Row-level feed validation uses generic identities, not selected pilot products."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from lxml import etree
from inventory_hub.services.supplier_availability_source import parse


class PartialAvailabilityTests(unittest.TestCase):
    def parse_xml(self, xml, cfg=None):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'stock.xml'
            path.write_text(xml)
            return parse(path, 'northfinder', cfg or {'feeds': {'sources': {'stock': {}}}}, 'stock', 'northfinder')

    def test_valid_leaves_survive_bad_unknown_and_duplicate_rows(self):
        result = self.parse_xml('''<products><product><reference>FAMILY</reference><quantity>900</quantity><variants>
          <variant><reference>ZERO</reference><quantity>0</quantity></variant>
          <variant><reference>OK</reference><quantity>12</quantity></variant>
          <variant><reference>BAD</reference><quantity>-1</quantity></variant>
          <variant><reference>UNKNOWN</reference></variant>
          <variant><reference>DUP</reference><quantity>5</quantity></variant>
          <variant><reference>DUP</reference><quantity>broken</quantity></variant>
          <variant><quantity>4</quantity></variant>
        </variants></product></products>''')
        self.assertEqual([item.sku for item in result], ['ZERO', 'OK'])
        self.assertFalse(result[0].available)
        self.assertEqual(str(result[1].quantity), '12')
        self.assertEqual(result.report(), {'total': 7, 'accepted': 2, 'rejected': 4, 'unknown': 1,
            'reasons': {'supplier_availability_invalid_quantity': 1, 'supplier_availability_unknown_quantity': 1,
                        'supplier_availability_duplicate_identity': 2, 'supplier_availability_invalid_identity': 1}})
        self.assertEqual(result.issues[0]['stock'], '-1')
        self.assertEqual([row['row'] for row in result.issues], [3, 4, 5, 6, 7])

    def test_single_products_and_custom_mapping_do_not_require_catalog_prices(self):
        cfg = {'feeds': {'sources': {'stock': {}}}, 'adapter_settings': {'northfinder_feed': {
            'item_tag': 'item', 'variant_tag': 'leaf', 'sku_field': 'sku', 'qty_field': 'stock'}}}
        rows = self.parse_xml('<root><item><sku>A</sku><stock>2</stock></item><item><variants><leaf><sku>B</sku><stock>0</stock></leaf></variants></item></root>', cfg)
        self.assertEqual([row.sku for row in rows], ['A', 'B'])
        self.assertEqual(rows.report()['rejected'], 0)

    def test_invalid_document_is_not_a_partial_success(self):
        for xml in ('<products><product>', '<html/>', '<!DOCTYPE products><products/>',
                    '<products><unexpected/></products>', '<products><product><variants><wrong/></variants></product></products>'):
            with self.subTest(xml=xml), self.assertRaises((ValueError, etree.LxmlError)):
                self.parse_xml(xml)

    def test_unknown_does_not_invent_zero_and_reports_are_bounded(self):
        rows = self.parse_xml('<products><product><reference>A</reference><quantity>0+</quantity></product>'
            '<product><reference>B</reference><quantity>https://example.invalid/private</quantity></product>'
            '<product><reference>C</reference><quantity>' + 'x' * 1000 + '</quantity></product></products>')
        self.assertEqual(len(rows), 0)
        self.assertEqual(rows.report()['unknown'], 1)
        self.assertEqual(rows.issues[1]['stock'], '[redacted]')
        self.assertEqual(len(rows.issues[2]['stock']), 120)
