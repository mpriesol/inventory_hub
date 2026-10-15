"""Synthetic SK Profi Bike feeds cover identity, variant and financial semantics."""
import copy
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from inventory_hub.adapters.sk_profi_bike_catalog import parse_catalog
from inventory_hub.catalog_types import ShopImportOptions
from inventory_hub.feed_mapping_types import MappingDefinition
from inventory_hub.services import feed_mapping
from inventory_hub.services.catalog_import import _prices, build_item
from inventory_hub.services.supplier_availability_source import AvailabilityError, configuration, parse as parse_availability


CFG = {"name": "SK Profi Bike", "adapter_settings": {
    "vat_rate": 23, "mapping": {"postprocess": {"product_code_prefix": "SKP-"}}}}


def option(code="0001", *, size="M", color="Blue", ean="0123456789012", retail="24.60", purchase="10", stock="3", extra=""):
    return f'''<option id="variant-{code}" atribute_1="{size}" atribute_2="{color}">
      <moc>{retail}</moc><voc>{purchase}</voc><sklad>{stock}</sklad><kod>{code}</kod><ean>{ean}</ean>{extra}
    </option>'''


def parent(options, *, group="family", axes=True):
    return f'''<product><id>{group}</id><name>Example glove</name><label>Short description</label>
      <manufacturer id="7">TEST BRAND</manufacturer><category id="25">Gloves</category>
      <category_tree>Clothing &amp;gt; Gloves</category_tree><description>Plain text</description>
      <description_formatted>&lt;p&gt;Formatted description&lt;/p&gt;</description_formatted>
      <atribute_1>{'Veľkosť' if axes else ''}</atribute_1><atribute_2>{'Farba' if axes else ''}</atribute_2>
      <options>{options}</options><vypredaj>0</vypredaj><url>https://supplier.example.test/gloves</url>
      <pictures><picture atribute_2="Blue">https://supplier.example.test/blue.jpg</picture>
        <picture atribute_2="Red">https://supplier.example.test/red.jpg</picture>
        <picture>https://supplier.example.test/common.jpg</picture>
        <picture atribute_1="L" atribute_2="Blue">https://supplier.example.test/large-blue.jpg</picture>
      </pictures><params><param><param_name>Material</param_name><param_value>&lt;b&gt;Nylon&lt;/b&gt;</param_value></param></params>
    </product>'''


class SkProfiBikeCatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "feed.xml"

    def parse(self, body, cfg=None):
        self.path.write_text("<products>" + body + "</products>", encoding="utf-8")
        return parse_catalog(self.path, "sk-profi-bike", cfg or CFG)

    def test_parent_context_exact_option_identity_and_scoped_pictures(self):
        rows = self.parse(parent(option() + option("0002", size="L", color="Red", ean="0987654321098")))
        first, raw = rows[0]
        second, _ = rows[1]
        self.assertEqual((first.code, first.shop_code, first.eans), ("0001", "SKP-0001", ["0123456789012"]))
        self.assertEqual(first.group_code, "family")
        self.assertEqual(first.variant_relationship, "explicit")
        self.assertEqual([(a.name, a.value) for a in first.variant_attributes], [("Veľkosť", "M"), ("Farba", "Blue")])
        self.assertEqual(first.images, ["https://supplier.example.test/blue.jpg", "https://supplier.example.test/common.jpg"])
        self.assertEqual(second.images, ["https://supplier.example.test/red.jpg", "https://supplier.example.test/common.jpg"])
        self.assertEqual(first.description, "<p>Formatted description</p>")
        self.assertEqual(first.short_description, "Short description")
        self.assertEqual((first.category, first.category_code), ("Clothing > Gloves", "25"))
        self.assertEqual(first.parameters[0].value, "Nylon")
        self.assertIn('<kod>0001</kod>', raw["xml"])
        self.assertNotIn('<kod>0002</kod>', raw["xml"])
        self.assertNotEqual(first.source_hash, second.source_hash)

    def test_net_voc_gross_moc_promo_purchase_and_retail_price_policy(self):
        product, raw = self.parse(parent(option(extra="<akcia>18.45</akcia><voc_akcia>8</voc_akcia>")))[0]
        self.assertEqual(product.prices.purchase_net, Decimal("8"))
        self.assertEqual(product.prices.purchase_gross, Decimal("9.84"))
        self.assertEqual(product.prices.retail_gross, Decimal("24.60"))
        self.assertEqual(product.prices.retail_net, Decimal("20.00"))
        self.assertEqual(product.prices.discount_net, Decimal("15.00"))
        self.assertIn("<voc>10</voc>", raw["xml"])
        price = _prices(product, ShopImportOptions(), CFG, True)[0]
        self.assertEqual(price["pricelists"][0]["price_original"], 24.6)
        self.assertEqual(price["price_purchase"], 9.84)
        cfg = copy.deepcopy(CFG)
        cfg["adapter_settings"]["price_coefficients"] = {"test brand": "0.9"}
        self.assertEqual(_prices(product, ShopImportOptions(), cfg, True)[0]["pricelists"][0]["price_original"], 22.14)

    def test_single_option_without_axes_is_standalone_and_empty_parent_not_a_product(self):
        product, _ = self.parse(parent("", group="empty") + parent(option(size="", color=""), axes=False))[0]
        self.assertEqual(product.code, "0001")
        self.assertIsNone(product.group_code)
        self.assertEqual(product.variant_relationship, "not_provided")
        item = build_item([product], ShopImportOptions(category_code="gloves"), CFG, True)
        self.assertEqual(item.status, "ready")
        self.assertNotIn("variants", item.payload)
        self.assertEqual(item.payload["code"], "SKP-0001")

    def test_missing_or_invalid_values_are_not_borrowed_from_another_option(self):
        first, raw = self.parse(parent(option(retail="", purchase="", ean="0000000000000", stock="-1") + option("0002")))[0]
        self.assertIsNone(first.prices.purchase_net)
        self.assertIsNone(first.prices.retail_gross)
        self.assertEqual(first.eans, [])
        self.assertIsNone(first.supplier_stock)
        self.assertIsNone(first.supplier_stock_raw)
        self.assertIn("invalid_number:sklad", first.warnings)
        self.assertIn("<sklad>-1</sklad>", raw["xml"])
        self.assertIn("missing_retail_price", first.warnings)
        self.assertIn("missing_ean", first.warnings)
        zero, _ = self.parse(parent(option(retail="0", purchase="0", stock="0")))[0]
        self.assertEqual(zero.prices.purchase_net, 0)
        self.assertEqual(zero.prices.retail_gross, 0)
        self.assertEqual(zero.supplier_stock, 0)

    def test_single_option_with_size_and_colour_is_not_an_artificial_variant(self):
        product, _ = self.parse(parent(option()))[0]
        self.assertIsNone(product.group_code)
        self.assertEqual(product.variant_attributes, [])
        self.assertIn(('Veľkosť', 'M'), [(p.name, p.value) for p in product.parameters])
        self.assertIn(('Farba', 'Blue'), [(p.name, p.value) for p in product.parameters])
        item = build_item([product], ShopImportOptions(), CFG, True)
        self.assertNotIn('variants', item.payload)
        self.assertEqual(item.payload['code'], 'SKP-0001')

    def test_invalid_promo_stays_unknown_and_missing_vat_does_not_invent_tax(self):
        product, _ = self.parse(parent(option(extra="<voc_akcia>bad</voc_akcia>")))[0]
        self.assertIsNone(product.prices.purchase_net)
        self.assertIn("invalid_number:voc_akcia", product.warnings)
        cfg = copy.deepcopy(CFG)
        del cfg["adapter_settings"]["vat_rate"]
        product, _ = self.parse(parent(option()), cfg)[0]
        self.assertEqual(product.prices.purchase_net, 10)
        self.assertIsNone(product.prices.purchase_gross)
        self.assertIsNone(product.prices.retail_net)
        self.assertIn("missing_vat", product.warnings)

    def test_duplicate_supplier_sku_is_blocked_and_loses_uncertain_price_and_stock(self):
        rows = self.parse(parent(option(), group="one") + parent(option(ean="0987654321098", purchase="15"), group="two"))
        self.assertEqual(len(rows), 1)
        product, raw = rows[0]
        self.assertEqual(product.import_blockers, ["duplicate_supplier_code"])
        self.assertEqual(product.eans, ["0123456789012", "0987654321098"])
        self.assertIsNone(product.prices.purchase_net)
        self.assertIsNone(product.prices.retail_gross)
        self.assertIsNone(product.supplier_stock)
        self.assertIsNone(product.group_code)
        self.assertIn("<conflicting_items>", raw["xml"])

    def test_native_mapping_retains_context_and_can_bind_selected_option_fields(self):
        self.parse(parent(option() + option("0002", size="L")))
        definition = MappingDefinition.model_validate({"record_path": "products/product", "bindings": [
            {"target": "manufacturer_code", "source": "options/option/kod"}]})
        mapped = feed_mapping.parse_mapped(self.path, "sk-profi-bike", CFG, "products", definition, 3, parse_catalog)
        self.assertEqual([p.manufacturer_code for p, _ in mapped], ["0001", "0002"])
        self.assertEqual(mapped[0][0].group_code, "family")
        self.assertEqual(mapped[0][0].mapping_revision, 3)
        self.assertEqual(mapped[0][0].prices.purchase_net, 10)
        self.assertFalse(feed_mapping.native_source_path(MappingDefinition(record_path="products/product/options/option"), parse_catalog))

    def test_invalid_structure_identity_and_xml_entities_rejected(self):
        for body in ("", "<unexpected/>", parent(option(code="")), parent("")):
            with self.subTest(body=body[:50]), self.assertRaises(ValueError):
                self.parse(body)
        self.path.write_text('<!DOCTYPE products [<!ENTITY bad SYSTEM "file:///etc/passwd">]><products/>', encoding="utf-8")
        with self.assertRaises(ValueError):
            parse_catalog(self.path, "sk-profi-bike", CFG)

    def test_stock_refresh_reads_minimal_structure_without_catalog_data(self):
        self.path.write_text('''<products><product><id>p</id><options>
            <option><kod>0001</kod><sklad>3</sklad></option>
            <option><kod>0002</kod><sklad>0</sklad></option>
            <option><kod>0003</kod><sklad>-1</sklad></option>
            <option><kod>0004</kod></option>
            <option><kod></kod><sklad>100</sklad></option>
            <option><sklad>5</sklad></option>
            </options></product></products>''', encoding="utf-8")
        cfg = {**CFG, "feeds": {"sources": {"availability": {"type": "stock", "catalog_parser": "sk-profi-bike",
               "remote": {"url": "https://supplier.example.test/availability.xml"}}}}}
        with patch("inventory_hub.services.catalog.supplier_config", return_value=cfg):
            self.assertEqual(configuration("sk-profi-bike", "availability")[2], "sk-profi-bike")
        rows = parse_availability(self.path, "sk-profi-bike", cfg, "availability", "sk-profi-bike")
        self.assertEqual([row.sku for row in rows], ["0001", "0002"])
        self.assertEqual((rows[0].quantity, rows[0].available, rows[0].quantity_kind), (3, True, "exact"))
        self.assertEqual((rows[1].quantity, rows[1].available), (0, False))
        self.assertEqual(rows.report()["total"], 6)
        self.assertEqual(rows.report()["rejected"], 3)
        self.assertEqual(rows.report()["unknown"], 1)
        self.assertEqual(rows.issues[0]["stock"], "-1")
        self.assertEqual(rows.issues[0]["code"], "supplier_availability_invalid_quantity")

    def test_stock_refresh_rejects_duplicates_malformed_and_unknown_only_feeds(self):
        cfg = {**CFG, "feeds": {"sources": {"stock": {"type": "stock"}}}}
        for options in (
            '<option><kod>A</kod><sklad>1</sklad></option><option><kod>A</kod><sklad>2</sklad></option>',
            '<option><kod>A</kod><sklad>bad</sklad></option>',
            '<option><kod>A</kod><sklad>NaN</sklad></option>',
            '<option><kod>A</kod><sklad>-1</sklad></option>',
            '<option><sklad>1</sklad></option>',
        ):
            self.path.write_text(f'<products><product><id>p</id><options>{options}</options></product></products>', encoding="utf-8")
            with self.subTest(options=options):
                result = parse_availability(self.path, "sk-profi-bike", cfg, "stock", "sk-profi-bike")
                self.assertEqual(len(result), 0)
                self.assertGreater(result.report()["rejected"], 0)
        for xml in ('<products><unexpected/></products>',
                    '<products><product><options><bad/></options></product></products>',
                    '<products><product><options/><options/></product></products>',
                    '<!DOCTYPE products [<!ENTITY bad SYSTEM "file:///etc/passwd">]><products/>'):
            self.path.write_text(xml, encoding="utf-8")
            with self.subTest(xml=xml), self.assertRaises(ValueError):
                parse_availability(self.path, "sk-profi-bike", cfg, "stock", "sk-profi-bike")


if __name__ == "__main__":
    unittest.main()
