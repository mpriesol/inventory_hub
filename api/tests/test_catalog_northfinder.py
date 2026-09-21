"""Synthetic Northfinder fixtures; no live supplier or shop requests."""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from catalog_fixtures import northfinder_product, northfinder_variant
from inventory_hub.adapters.northfinder_catalog import parse_catalog
from inventory_hub.catalog_types import ShopImportOptions
from inventory_hub.services.catalog_import import build_item


class NorthfinderCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "northfinder.xml"
        self.cfg = {"adapter_settings": {"vat": 23, "product_code_prefix": "NF-"}}

    def parse(self, body):
        self.path.write_text(body)
        rows = parse_catalog(self.path, "northfinder", self.cfg)
        for i, (p, _) in enumerate(rows, 1):
            p.id = i
        return rows

    def test_explicit_variants_images_parameters_raw_context_and_net_purchase(self):
        rows = self.parse("<products>" + northfinder_product(northfinder_variant() + northfinder_variant("N-RED-L", "000002", "L")) + "</products>")
        p, raw = rows[0]
        self.assertEqual(p.group_code, "N")
        self.assertEqual(p.shop_code, "NF-N-RED-M")
        self.assertEqual(p.eans, ["000001"])
        self.assertEqual(p.prices.purchase_gross, Decimal("12.30"))
        self.assertEqual(p.prices.retail_net, Decimal("20.00"))
        self.assertEqual(p.images, ["https://b2b.northfinder.com/synthetic-test.jpg"])
        self.assertNotIn("blue", [a.value for a in p.parameters])
        self.assertIn("preserve parent", raw["xml"])
        self.assertIn("preserve variant", raw["xml"])
        self.assertNotIn("N-RED-L", raw["xml"], "Raw item has its parent context, not all siblings")
        item = build_item([r[0] for r in rows], ShopImportOptions(), self.cfg, True)
        self.assertEqual(item.status, "ready")
        self.assertEqual(item.payload["prices"][0]["pricelists"][0]["price_original"], 24.6)
        self.assertEqual(len(item.payload["variants"]), 2)
        self.assertEqual(item.payload["parameters"][0]["descriptions"][0]["name"], "Materiál")

    def test_missing_variant_prices_inherit_parent_and_are_marked(self):
        p, raw = self.parse("<products>" + northfinder_product(northfinder_variant(purchase="", retail="")) + "</products>")[0]
        self.assertEqual(p.prices.purchase_net, 10)
        self.assertEqual(p.prices.retail_gross, Decimal("24.60"))
        self.assertIn("inherited_purchase_price", p.warnings)
        self.assertIn("inherited_retail_price", p.warnings)
        self.assertIn("<price/>", raw["xml"])
        self.assertEqual(build_item([p], ShopImportOptions(), {}, True).status, "ready")

    def test_missing_parent_price_requires_manual_sale_price_without_inventing_rrp(self):
        p, _ = self.parse("<products>" + northfinder_product(northfinder_variant(purchase="", retail=""), purchase="", retail="") + "</products>")[0]
        self.assertIsNone(p.prices.purchase_net)
        self.assertIsNone(p.prices.retail_gross)
        self.assertEqual(build_item([p], ShopImportOptions(), {}, True).status, "invalid")
        item = build_item([p], ShopImportOptions(), {}, True, {p.id: Decimal("19.99")})
        self.assertEqual(item.status, "ready")
        self.assertNotIn("price_common", item.payload["prices"][0])
        self.assertNotIn("price_purchase", item.payload["prices"][0])

    def test_explicit_zero_and_invalid_prices_do_not_inherit(self):
        p, _ = self.parse("<products>" + northfinder_product(northfinder_variant(purchase="NaN", retail="0")) + "</products>")[0]
        self.assertIsNone(p.prices.purchase_net)
        self.assertEqual(p.prices.retail_gross, 0)
        self.assertNotIn("inherited_purchase_price", p.warnings)
        self.assertNotIn("inherited_retail_price", p.warnings)
        self.assertEqual(build_item([p], ShopImportOptions(), {}, True).status, "invalid")

    def test_conflicting_codes_preserve_candidates_but_block_even_with_manual_price(self):
        rows = self.parse("<products>" + northfinder_product(northfinder_variant() + northfinder_variant(ean="000002")) + "</products>")
        self.assertEqual(len(rows), 1)
        p, raw = rows[0]
        self.assertEqual(p.eans, ["000001", "000002"])
        self.assertEqual(p.import_blockers, ["duplicate_supplier_code"])
        self.assertIsNone(p.prices.retail_gross)
        self.assertIn("<conflicting_items>", raw["xml"])
        self.assertIn("000002", raw["xml"])
        item = build_item([p], ShopImportOptions(), {}, True, {p.id: Decimal("50")})
        self.assertEqual(item.status, "invalid")
        self.assertEqual(item.payload, {})
        self.assertIn("duplicate_supplier_code", item.errors)

    def test_rrp_below_purchase_warns_and_does_not_change_price(self):
        p, _ = self.parse("<products>" + northfinder_product(northfinder_variant(purchase="20", retail="15")) + "</products>")[0]
        self.assertIn("retail_below_purchase", p.warnings)
        item = build_item([p], ShopImportOptions(), {}, True)
        self.assertEqual(item.status, "ready")
        self.assertIn("sale_below_purchase", item.warnings)
        self.assertEqual(item.payload["prices"][0]["pricelists"][0]["price_original"], 15)

    def test_rejects_wrong_root_entities_and_missing_identity(self):
        for xml in ("<products/>", "<stock/>", '<!DOCTYPE products [<!ENTITY x SYSTEM "file:///unreadable">]><products>' + northfinder_product() + '</products>',
                    "<products>" + northfinder_product().replace("<reference>N-RED-M</reference>", "<reference/>") + "</products>"):
            with self.subTest(xml=xml[:30]), self.assertRaises(ValueError):
                self.parse(xml)
