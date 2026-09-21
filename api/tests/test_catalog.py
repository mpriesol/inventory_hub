import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from catalog_fixtures import xml_item
from inventory_hub.adapters.paul_lange_catalog import parse_catalog
from inventory_hub.adapters.pl_feed_convert import convert_xml_to_upgates
from inventory_hub.services.catalog import folded, _http_url
from inventory_hub.services.catalog_html import clean_description


class CatalogParserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "feed.xml"
        self.cfg = {"adapter_settings": {"mapping": {"postprocess": {"product_code_prefix": "PL-"}}, "vat": 23}}

    def parse(self, value):
        self.path.write_text(value, encoding="utf-8")
        return parse_catalog(self.path, "paul-lange", self.cfg)

    def test_preserves_identifiers_parameters_source_and_stock_meaning(self):
        rows = self.parse("<SHOP>" + xml_item(ean="00012345/00123456") + "</SHOP>")
        p, source = rows[0]
        self.assertEqual(p.eans, ["00012345", "00123456"])
        self.assertEqual(p.shop_code, "PL-A-001")
        self.assertEqual(p.prices.purchase_net, Decimal("60"))
        self.assertEqual(len(p.images), 2)
        self.assertEqual([(a.name, a.value) for a in p.parameters], [("Farba", "červená"), ("Veľkosť", "M")])
        self.assertIsNone(p.supplier_stock)
        self.assertEqual(p.supplier_stock_min, 6)
        self.assertTrue(p.supplier_external_available)
        self.assertIn("<UNUSED>original</UNUSED>", source["xml"])
        self.assertEqual(source["fields"]["STA_PARAMS"][0]["UNUSED"], ["original"])
        self.assertEqual(folded(p.name), "cervena prilba")

    def test_never_infers_variant_groups_from_names(self):
        rows = self.parse("<SHOP>" + xml_item() + xml_item("A-002", ean="00012346") + "</SHOP>")
        self.assertTrue(all(p.group_code is None and not p.variant_attributes for p, _ in rows))
        rows = self.parse("<SHOP>" + xml_item(extra="<ITEMGROUP_ID>GROUP-1</ITEMGROUP_ID>") + "</SHOP>")
        self.assertEqual(rows[0][0].variant_relationship, "explicit")
        self.assertEqual(rows[0][0].group_code, "GROUP-1")

    def test_rejects_wrong_feed_duplicates_empty_and_entities(self):
        for xml in ("<STOCK/>", "<SHOP/>", "<SHOP>" + xml_item() * 2 + "</SHOP>",
                    '<!DOCTYPE SHOP [<!ENTITY x SYSTEM "file:///unreadable">]><SHOP>' + xml_item(name="&x;") + "</SHOP>"):
            with self.subTest(xml=xml[:40]):
                with self.assertRaises(ValueError):
                    self.parse(xml)

    def test_missing_and_malformed_prices_are_not_zero(self):
        p, _ = self.parse("<SHOP>" + xml_item().replace("<PRICE>100</PRICE>", "<PRICE>NaN</PRICE>").replace("<PRICE_VAT>123</PRICE_VAT>", "<PRICE_VAT/>") + "</SHOP>")[0]
        self.assertIsNone(p.prices.retail_net)
        self.assertIsNone(p.prices.retail_gross)
        self.assertIn("missing_retail_price", p.warnings)

    def test_existing_csv_mapping_stays_compatible(self):
        self.parse("<SHOP>" + xml_item() + "</SHOP>")
        output = Path(self.temp.name) / "feed.csv"
        convert_xml_to_upgates(self.path, output, prefix="PL-")
        import csv
        with output.open(encoding="utf-8-sig", newline="") as handle:
            data = list(csv.DictReader(handle, delimiter=";"))
        self.assertEqual(data[0]["[PRODUCT_CODE]"], "PL-A-001")
        self.assertEqual(data[0]["[TITLE]"], "Červená prilba")

    def test_supplier_html_has_no_executable_content(self):
        clean = clean_description('<script>alert(1)</script><svg><script>x</script></svg><p onclick="bad()">Safe <b>text</b><a href="javascript:bad()">link</a><a href="https://example.com">web</a><img src=x onerror=bad()></p>')
        self.assertIn("<b>text</b>", clean)
        for forbidden in ("script", "onclick", "onerror", "javascript:", "<svg", "<img"):
            self.assertNotIn(forbidden, clean)

    def test_shop_links_accept_only_http_urls_without_credentials(self):
        self.assertEqual(_http_url("https://shop.example.test/p/product"), "https://shop.example.test/p/product")
        for value in (None, "javascript:alert(1)", "//shop.example.test/p", "https://user:password@shop.example.test/p", "https://shop.example.test/\n", "https://[invalid"):
            self.assertIsNone(_http_url(value))
