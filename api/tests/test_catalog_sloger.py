"""Synthetic Sloger listing fixtures; no live supplier or shop requests."""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from inventory_hub.adapters.sloger_catalog import parse_catalog
from inventory_hub.catalog_types import ShopImportOptions
from inventory_hub.feed_mapping_types import MappingDefinition
from inventory_hub.services.catalog_import import build_item
from inventory_hub.services.feed_mapping import parse_mapped


def option(code="SHIRT-M", name="Veľkosť M", ean="0123456789012", price="24.60"):
    return f"""<option><id>501</id><code>{code}</code><name>{name}</name><ean>{ean}</ean>
        <price_msrp>{price}</price_msrp><price_msrp_czk>999</price_msrp_czk></option>"""


def product(options="", *, code="SHIRT", parent_id="101", price="24.60", ean="0001234567890"):
    return f"""<product><id>{parent_id}</id><code>{code}</code><name>Test shirt</name>
        <ean>{ean}</ean><manufacturer>Test Brand</manufacturer>
        <description>&lt;p&gt;Original parent description&lt;/p&gt;</description>
        <category>Oblečenie / Tričká</category><price_msrp>{price}</price_msrp>
        <price_msrp_czk>12345</price_msrp_czk><images><url>https://supplier.example/image.jpg</url></images>
        <param><param_name>Materiál</param_name><val>bavlna</val></param>
        <param><param_name>Farba</param_name><val>modrá</val></param>
        <options>{options}</options><video>https://supplier.example/video</video></product>"""


class SlogerCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "sloger.xml"
        self.cfg = {"adapter_settings": {"vat_rate": 23, "product_code_prefix": "SL-",
                                        "price_coefficients": {"TEST BRAND": 1}}}

    def parse(self, body):
        self.path.write_text(body, encoding="utf-8")
        rows = parse_catalog(self.path, "sloger", self.cfg, "test-brand")
        for index, (p, _) in enumerate(rows, 1):
            p.id = index
        return rows

    def test_standalone_dmoc_only_has_no_purchase_or_supplier_stock(self):
        p, raw = self.parse("<products>" + product(code="00123") + "</products>")[0]
        self.assertEqual(p.shop_code, "SL-00123")
        self.assertEqual(p.eans, ["0001234567890"])
        self.assertEqual(p.prices.currency, "EUR")
        self.assertEqual(p.prices.retail_gross, Decimal("24.60"))
        self.assertEqual(p.prices.retail_net, Decimal("20.00"))
        self.assertIsNone(p.prices.purchase_net)
        self.assertIsNone(p.prices.purchase_gross)
        self.assertIsNone(p.supplier_stock)
        self.assertIsNone(p.availability)
        self.assertIsNone(p.group_code)
        self.assertIn("video", raw["fields"])
        self.assertEqual([(a.name, a.value) for a in p.parameters], [("Materiál", "bavlna"), ("Farba", "modrá")])
        for with_vat, expected in [(True, 24.6), (False, 20)]:
            item = build_item([p], ShopImportOptions(), self.cfg, with_vat)
            self.assertEqual(item.status, "ready")
            self.assertEqual(item.payload["prices"][0]["pricelists"][0]["price_original"], expected)
            self.assertNotIn("price_purchase", item.payload["prices"][0])
            self.assertFalse(item.payload["active_yn"])

    def test_options_preserve_parent_context_and_explicit_size_axis(self):
        rows = self.parse("<products>" + product(option() + option("SHIRT-L", "Veľkosť :L", "0987654321098", "30.75")) + "</products>")
        p, raw = rows[0]
        self.assertEqual(p.group_code, "101")
        self.assertEqual(p.variant_relationship, "explicit")
        self.assertEqual([(a.name, a.value) for a in p.variant_attributes], [("Veľkosť", "M")])
        self.assertIn("Original parent description", raw["xml"])
        self.assertIn("SHIRT-M", raw["xml"])
        self.assertNotIn("SHIRT-L", raw["xml"])
        self.assertEqual(p.images, ["https://supplier.example/image.jpg"])
        item = build_item([p for p, _ in rows], ShopImportOptions(), self.cfg, True)
        self.assertEqual(item.status, "ready")
        self.assertEqual(item.code, "SL-G-101")
        self.assertEqual({v["code"] for v in item.payload["variants"]}, {"SL-SHIRT-M", "SL-SHIRT-L"})
        self.assertTrue(all("price_purchase" not in v["prices"][0] for v in item.payload["variants"]))

    def test_configured_vat_rate_precedes_legacy_settings_and_preserves_zero(self):
        self.cfg["vat_rate"] = 10
        cases = [({"vat_rate": 20, "vat": 5}, 20, "100.00"),
                 ({"vat_rate": 0, "vat": 23}, 0, "120.00"),
                 ({"vat": 20}, 20, "100.00"), ({}, 10, "109.09")]
        for settings, vat, net in cases:
            with self.subTest(settings=settings):
                self.cfg["adapter_settings"] = {"product_code_prefix": "SL-", **settings}
                p, _ = self.parse("<products>" + product(price="120.00") + "</products>")[0]
                self.assertEqual(p.prices.vat_percent, vat)
                self.assertEqual(p.prices.retail_gross, Decimal("120.00"))
                self.assertEqual(p.prices.retail_net, Decimal(net))
                self.assertEqual(build_item([p], ShopImportOptions(), self.cfg, False).status, "ready")

    def test_reused_parent_code_keeps_unrelated_families_separate(self):
        rows = self.parse("<products>" + product(option(), parent_id="101")
                          + product(option("FRAME-M"), parent_id="102") + "</products>")
        self.assertEqual([p.group_code for p, _ in rows], ["101", "102"])

    def test_unlabelled_mixed_options_are_preserved_without_invented_axes(self):
        rows = self.parse("<products>" + product(option(name="Veľkosť M") + option("B", "red / large")) + "</products>")
        self.assertEqual([p.variant_attributes[0].name for p, _ in rows], ["Variant", "Variant"])
        self.assertEqual([p.variant_attributes[0].value for p, _ in rows], ["Veľkosť M", "red / large"])
        self.assertEqual(build_item([p for p, _ in rows], ShopImportOptions(), self.cfg, True).status, "ready")

    def test_missing_option_name_and_duplicate_names_block_variant_publication(self):
        for variants in (option(name=""), option() + option("B")):
            with self.subTest(variants=variants):
                rows = self.parse("<products>" + product(variants) + "</products>")
                item = build_item([p for p, _ in rows], ShopImportOptions(), self.cfg, True)
                self.assertEqual(item.status, "invalid")
                self.assertIn("variant_attributes_ambiguous", item.errors)

    def test_missing_prices_inherit_but_zero_or_invalid_values_do_not(self):
        p, _ = self.parse("<products>" + product(option(price="")) + "</products>")[0]
        self.assertEqual(p.prices.retail_gross, Decimal("24.60"))
        self.assertIn("inherited_retail_price", p.warnings)
        for price in ("0", "NaN", "-1", "broken"):
            with self.subTest(price=price):
                p, _ = self.parse("<products>" + product(option(price=price)) + "</products>")[0]
                self.assertNotIn("inherited_retail_price", p.warnings)
                self.assertIn("missing_retail_price", p.warnings)
                self.assertEqual(build_item([p], ShopImportOptions(), self.cfg, True).status, "invalid")

    def test_placeholder_or_missing_option_ean_never_inherits_parent(self):
        for ean in ("", "0", "0000000000000"):
            with self.subTest(ean=ean):
                p, _ = self.parse("<products>" + product(option(ean=ean)) + "</products>")[0]
                self.assertEqual(p.eans, [])
                self.assertIn("missing_ean", p.warnings)

    def test_duplicate_supplier_codes_retain_evidence_but_block_manual_price(self):
        rows = self.parse("<products>" + product(option() + option(ean="0987654321098")) + "</products>")
        self.assertEqual(len(rows), 1)
        p, raw = rows[0]
        self.assertEqual(p.import_blockers, ["duplicate_supplier_code"])
        self.assertEqual(p.eans, ["0123456789012", "0987654321098"])
        self.assertIsNone(p.prices.retail_gross)
        self.assertIn("conflicting_items", raw["xml"])
        item = build_item([p], ShopImportOptions(), self.cfg, True, {p.id: Decimal("24.60")})
        self.assertEqual(item.status, "invalid")
        self.assertEqual(item.payload, {})

    def test_mapping_overlay_keeps_native_option_identity(self):
        self.parse("<products>" + product(option() + option("SHIRT-L", "Veľkosť L")) + "</products>")
        definition = MappingDefinition(format="xml", record_path="products/product",
                                       bindings=[{"target": "brand", "constant": "Mapped Brand"}])
        rows = parse_mapped(self.path, "sloger", self.cfg, "test-brand", definition, 1, parse_catalog)
        self.assertEqual([p.code for p, _ in rows], ["SHIRT-M", "SHIRT-L"])
        self.assertTrue(all(p.group_code == "101" and p.brand == "Mapped Brand" for p, _ in rows))

    def test_empty_native_mapping_keeps_option_labels_and_context(self):
        self.parse("<products>" + product(option()) + "</products>")
        rows = parse_mapped(self.path, "sloger", self.cfg, "products", MappingDefinition(), 1, parse_catalog)
        p, _ = rows[0]
        self.assertEqual(p.code, "SHIRT-M")
        self.assertEqual(p.group_code, "101")
        self.assertEqual(p.raw_fields["options"][0]["option"][0]["name"], ["Veľkosť M"])
        self.assertEqual(p.raw_fields["description"], ["<p>Original parent description</p>"])
        self.assertEqual([(a.name, a.value) for a in p.variant_attributes], [("Veľkosť", "M")])
        self.assertIsNone(p.prices.purchase_net)

    def test_rejects_empty_wrong_root_dtd_and_missing_identity(self):
        for xml in ("<products/>", "<stock/>",
                    '<!DOCTYPE products [<!ENTITY x SYSTEM "file:///unreadable">]><products>' + product() + '</products>',
                    "<products>" + product(option(code="")) + "</products>",
                    "<products>" + product(option(), parent_id="") + "</products>"):
            with self.subTest(xml=xml[:50]), self.assertRaises(ValueError):
                self.parse(xml)
