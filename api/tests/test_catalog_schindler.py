"""Synthetic supplier fixtures; no live feeds, credentials or shop writes."""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from lxml import etree

from inventory_hub.adapters.schindler_catalog import parse_catalog
from inventory_hub.catalog_types import ShopImportOptions
from inventory_hub.services.catalog_import import build_item
from inventory_hub.services.supplier_availability_source import parse as parse_stock


def item(code="00123", name="Duša Test", extra="", purchase="6.37", retail="10.90", stock="0"):
    return f"""<SHOPITEM><ITEM_ID>987</ITEM_ID><CODE>{code}</CODE><PRODUCTNAME>{name}</PRODUCTNAME>
      <EAN>04012345678901</EAN><MANUFACTURER>Test</MANUFACTURER><VAT>23</VAT>
      <PURCHASE_PRICE>{purchase}</PURCHASE_PRICE><CUSTOMER_PRICE>{retail}</CUSTOMER_PRICE>
      <CATALOGUE_PRICE>12.90</CATALOGUE_PRICE><STOCK_ITEM>{stock}</STOCK_ITEM>
      <CATEGORYTEXT>Komponenty | Duše</CATEGORYTEXT><URL>https://supplier.example.test/product</URL>
      <DESCRIPTION>&lt;p&gt;Opis&lt;/p&gt;</DESCRIPTION><DESCRIPTION_SHORT>Krátky opis</DESCRIPTION_SHORT>
      <IMGURL>https://supplier.example.test/main.jpg</IMGURL>
      <IMGURL_ALTERNATIVE>https://supplier.example.test/other.jpg</IMGURL_ALTERNATIVE>
      <PARAM><PARAM_NAME>Farba</PARAM_NAME><VAL>Čierna</VAL></PARAM>{extra}</SHOPITEM>"""


def warehouse(name="central", value="10", greater="true"):
    return f"""<WAREHOUSE><WAREHOUSE_NAME>{name}</WAREHOUSE_NAME><QUANTITY>
      <IS_GREATER_THAN>{greater}</IS_GREATER_THAN><VALUE>{value}</VALUE></QUANTITY></WAREHOUSE>"""


class SchindlerCatalogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "feed.xml"
        self.cfg = {"product_code_prefix": "SCH-", "feeds": {"sources": {"products": {"type": "products"}}}}

    def write(self, body):
        self.path.write_text("<SHOP>" + body + "</SHOP>")

    def products(self, body):
        self.write(body)
        rows = parse_catalog(self.path, "schindler", self.cfg)
        for index, (product, _) in enumerate(rows, 1):
            product.id = index
        return rows

    def stock(self):
        return parse_stock(self.path, "schindler", self.cfg, "products", "schindler")

    def test_identity_gross_prices_and_source_facts_survive_without_arbitrary_shop_metas(self):
        p, raw = self.products(item())[0]
        self.assertEqual((p.code, p.shop_code, p.eans), ("00123", "SCH-00123", ["04012345678901"]))
        self.assertEqual((p.prices.purchase_gross, p.prices.purchase_net), (Decimal("6.37"), Decimal("5.18")))
        self.assertEqual((p.prices.retail_gross, p.prices.retail_net), (Decimal("10.90"), Decimal("8.86")))
        self.assertIsNone(p.prices.discount_gross)
        self.assertEqual(p.metadata, {})
        self.assertEqual(raw["fields"]["CATALOGUE_PRICE"], ["12.90"])
        self.assertEqual(p.category, "Komponenty | Duše")
        self.assertEqual(p.description, "<p>Opis</p>")
        self.assertEqual(len(p.images), 2)
        self.assertEqual([(a.name, a.value) for a in p.parameters], [("Farba", "Čierna")])
        for with_vat, purchase in ((True, 6.37), (False, 5.18)):
            preview = build_item([p], ShopImportOptions(), self.cfg, with_vat)
            self.assertEqual(preview.status, "ready")
            self.assertEqual(preview.payload["prices"][0]["price_purchase"], purchase)
            self.assertFalse(preview.payload["active_yn"])
            self.assertNotIn("stock", preview.payload)
            self.assertNotIn("stock_increment", preview.payload)

    def test_only_explicit_real_families_become_variants(self):
        rows = self.products(item(extra="<ITEMGROUP_ID>G</ITEMGROUP_ID><VARIANT>42 mm</VARIANT>") +
            item("00456", "Duša Test 60 mm", "<ITEMGROUP_ID>G</ITEMGROUP_ID><VARIANT>60 mm</VARIANT>") +
            item("00789", extra="<ITEMGROUP_ID>SINGLE</ITEMGROUP_ID><VARIANT>Čierna</VARIANT>") + item("OTHER"))
        family = [p for p, _ in rows[:2]]
        preview = build_item(family, ShopImportOptions(), self.cfg, True)
        self.assertEqual(preview.status, "ready")
        self.assertEqual(len(preview.payload["variants"]), 2)
        self.assertTrue(all(not p["main_yn"] for p in preview.payload["variants"]))
        singleton = rows[2][0]
        self.assertIsNone(singleton.group_code)
        self.assertEqual(singleton.variant_attributes, [])
        self.assertIn(("Variant", "Čierna"), [(a.name, a.value) for a in singleton.parameters])
        self.assertIsNone(rows[3][0].group_code)

    def test_missing_or_duplicate_variant_labels_remain_blocked_in_import_preview(self):
        for labels in (("42 mm", "42 mm"), ("42 mm", "")):
            rows = self.products("".join(item(str(i), extra=f"<ITEMGROUP_ID>G</ITEMGROUP_ID><VARIANT>{label}</VARIANT>")
                                         for i, label in enumerate(labels)))
            preview = build_item([p for p, _ in rows], ShopImportOptions(), self.cfg, True)
            self.assertEqual(preview.status, "invalid")
            self.assertIn("variant_attributes_ambiguous", preview.errors)
            self.assertEqual(preview.payload, {})

    def test_greater_than_remains_minimum_and_external_stock_is_separate(self):
        warehouses = "<WAREHOUSES>" + warehouse() + warehouse("internal", "2", "false") + warehouse("external", "10", "true") + "</WAREHOUSES>"
        p, _ = self.products(item(extra=warehouses))[0]
        self.assertIsNone(p.supplier_stock)
        self.assertEqual(p.supplier_stock_min, 13)
        self.assertEqual(p.supplier_stock_raw, "13+")
        self.assertIsNone(p.supplier_stock_external)
        self.assertEqual(p.supplier_stock_external_raw, "11+")
        observation = self.stock()[0]
        self.assertEqual((observation.quantity, observation.quantity_kind), (13, "minimum"))
        self.assertEqual(observation.raw["external_stock"], "11+")
        self.products(item(extra="<WAREHOUSES>" + warehouse("external", "3", "false") + "</WAREHOUSES>"))
        observation = self.stock()[0]
        self.assertTrue(observation.available)
        self.assertEqual(observation.quantity, 3)
        self.assertEqual(observation.raw["stock"], "0")

    def test_bad_stock_rows_are_reported_while_valid_rows_survive_bad_catalog_prices(self):
        self.write(item("GOOD", purchase="NaN", retail="", stock="4") +
                   item("BAD", extra="<WAREHOUSES>" + warehouse(value="-2") + "</WAREHOUSES>") +
                   item("UNKNOWN", stock="") + item("DUP") + item("DUP"))
        stock = self.stock()
        self.assertEqual([r.sku for r in stock], ["GOOD"])
        self.assertEqual(stock.report(), {"total": 5, "accepted": 1, "rejected": 3, "unknown": 1,
            "reasons": {"supplier_availability_invalid_quantity": 1, "supplier_availability_unknown_quantity": 1,
                        "supplier_availability_duplicate_identity": 2}})

    def test_zero_unknown_and_invalid_stock_never_become_fabricated_positive_stock(self):
        for value, expected, unknown in (("0", False, False), ("", None, True), ("bad", None, True)):
            p, _ = self.products(item(stock=value))[0]
            self.assertIs(p.supplier_available, expected)
            result = self.stock()
            if unknown:
                self.assertEqual(len(result), 0)
                self.assertEqual(len(result.issues), 1)
            else:
                self.assertEqual(result[0].quantity, 0)
        p, _ = self.products(item(extra="<WAREHOUSES>" + warehouse("unexpected") + "</WAREHOUSES>"))[0]
        self.assertIsNone(p.supplier_available)
        self.assertIn("invalid_number:STOCK_ITEM", p.warnings)

    def test_invalid_prices_warn_without_inventing_cost_and_feed_vat_is_required(self):
        p, _ = self.products(item(purchase="NaN", retail="0"))[0]
        self.assertIsNone(p.prices.purchase_net)
        self.assertIn("invalid_number:PURCHASE_PRICE", p.warnings)
        self.assertEqual(build_item([p], ShopImportOptions(), self.cfg, True).status, "invalid")
        p, _ = self.products(item().replace("<VAT>23</VAT>", "<VAT/>"))[0]
        self.assertIsNone(p.prices.vat_percent)
        self.assertIn("missing_vat", p.warnings)
        self.assertEqual(build_item([p], ShopImportOptions(), self.cfg, True).errors, ["missing_vat"])

    def test_identity_and_structural_failures_do_not_create_catalog(self):
        for body in (item() + item(), item(code=""), item(name=""), ""):
            with self.subTest(body=body[:20]), self.assertRaises(ValueError):
                self.products(body)
        for xml in ("<OTHER/>", "<SHOP><SHOPITEM>", '<!DOCTYPE SHOP [<!ENTITY x SYSTEM "file:///unreadable">]><SHOP>' + item() + '</SHOP>'):
            self.path.write_text(xml)
            with self.assertRaises((ValueError, etree.LxmlError)):
                parse_catalog(self.path, "schindler", self.cfg)
            with self.assertRaises((ValueError, etree.LxmlError)):
                self.stock()
