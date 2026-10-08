"""Declarative mapping preserves feed facts and rejects ambiguous identities."""
import json
import tempfile
import unittest
from pathlib import Path

from catalog_fixtures import northfinder_product, northfinder_variant, product, xml_item
from inventory_hub.adapters.paul_lange_catalog import parse_catalog
from inventory_hub.adapters.northfinder_catalog import parse_catalog as parse_northfinder
from inventory_hub.feed_mapping_types import MappingDefinition
from inventory_hub.services import feed_mapping as mapping
from inventory_hub.services.feed_mapping_source import inspect_records, read_records, values_at

CFG = {"default_currency": "EUR", "adapter_settings": {"vat": 23, "mapping": {"postprocess": {"product_code_prefix": "PL-"}}}}


class FeedMappingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "feed.bin"

    def write(self, content):
        self.path.write_text(content, encoding="utf-8")
        return self.path

    def test_inspect_complete_xml_paths_repeated_parameters_and_original_records(self):
        self.write("<SHOP>" + xml_item(extra='<CUSTOM special="yes"><Unknown>kept</Unknown></CUSTOM>') + xml_item("A-002", ean="00002") + "</SHOP>")
        records, kind, path = read_records(self.path, MappingDefinition())
        result = inspect_records(records, kind, path)
        self.assertEqual((kind, path, len(records)), ("xml", "SHOP/SHOPITEM", 2))
        fields = {field["path"]: field for field in result["fields"]}
        self.assertEqual(fields["CUSTOM/Unknown"]["populated"], 1)
        self.assertIn("CUSTOM/@attributes/special", fields)
        self.assertEqual(values_at(records[0]["fields"], "DYN_PARAMS/PARAM/DESC"), ["Farba", "Veľkosť"])
        self.assertIn("<Unknown>kept</Unknown>", records[0]["xml"])

    def test_native_seed_keeps_identity_prices_groups_and_unknown_fields(self):
        self.write("<SHOP>" + xml_item(extra="<ITEMGROUP_ID>family</ITEMGROUP_ID><NEW_FIELD>unknown</NEW_FIELD>") + "</SHOP>")
        definition = MappingDefinition.model_validate({"bindings": [{"target": "parameter:Extra", "source": "NEW_FIELD"}]})
        result, raw = mapping.parse_mapped(self.path, "paul-lange", CFG, "products", definition, 2, parse_catalog)[0]
        self.assertEqual((result.shop_code, result.group_code, result.variant_relationship), ("PL-A-001", "family", "explicit"))
        self.assertEqual(str(result.prices.purchase_net), "60")
        self.assertIn({"name": "Extra", "value": "unknown"}, [p.model_dump() for p in result.parameters])
        self.assertEqual(result.raw_fields, raw["fields"])
        self.assertEqual(result.mapping_revision, 2)
        self.assertEqual(result.seo_title, result.name)
        self.assertNotIn("<", result.seo_description)

    def test_generic_csv_leading_zeros_numeric_vat_and_no_stock_authority(self):
        self.write('sku;title;ean;net;qty\n00012;Duša;000123;10,00;6\n')
        definition = MappingDefinition.model_validate({"bindings": [
            {"target": "code", "source": "sku"}, {"target": "name", "source": "title"},
            {"target": "eans", "source": "ean"}, {"target": "prices.purchase_net", "source": "net"},
            {"target": "supplier_stock", "source": "qty"}]})
        result, _ = mapping.parse_mapped(self.path, "sample", CFG, "products", definition, 1)[0]
        self.assertEqual((result.code, result.shop_code, result.eans), ("00012", "PL-00012", ["000123"]))
        self.assertEqual(str(result.prices.purchase_gross), "12.30")
        self.assertEqual(result.supplier_stock, 6)
        self.assertIsNone(result.prices.retail_gross)
        self.assertNotIn("stock", result.model_dump())
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            mapping.validate_definition(MappingDefinition.model_validate({"bindings": [{"target": "stock", "source": "qty"}]}))

    def test_nested_json_params_value_maps_images_and_seo(self):
        self.write(json.dumps({"products": [{"sku": "01", "name": "Duša", "description": "<p>Duša pre bicykel</p>",
            "props": [{"n": "ventil", "v": "SV"}, {"n": "material", "v": "butyl"}],
            "valve": "SV", "photos": ["https://example.test/1.jpg", "https://example.test/2.jpg"]}]}))
        definition = MappingDefinition.model_validate({"bindings": [{"target": "code", "source": "sku"},
            {"target": "name", "source": "name"}, {"target": "description", "source": "description"},
            {"target": "parameters", "source": "props", "param_name_path": "n", "param_value_path": "v"},
            {"target": "parameter:Ventil", "source": "valve", "transforms": [{"op": "map", "values": {"SV": "Presta"}}]},
            {"target": "images", "source": "photos"}]})
        result, _ = mapping.parse_mapped(self.path, "sample", CFG, "products", definition, 1)[0]
        self.assertEqual(result.parameters[-1].value, "Presta")
        self.assertEqual(len(result.images), 2)
        self.assertEqual(result.seo_description, "Duša pre bicykel")

    def test_zero_is_preserved_and_ambiguous_or_malformed_values_rejected(self):
        definition = MappingDefinition.model_validate({"bindings": [{"target": "prices.retail_gross", "source": "price", "default": "99"}]})
        result = mapping.apply_definition(product(), {"price": "0"}, definition, "paul-lange", "products", CFG)
        self.assertEqual(result.prices.retail_gross, 0)
        for value in ("bad", "NaN", "-1", ["5", "6"]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                mapping.apply_definition(product(), {"price": value}, definition, "paul-lange", "products", CFG)
        identifier = MappingDefinition.model_validate({"bindings": [{"target": "code", "source": "sku"}]})
        for value in (123, ["one", "two"]):
            with self.assertRaises(ValueError):
                mapping.apply_definition(product(), {"sku": value}, identifier, "paul-lange", "products", CFG)

    def test_unknown_supplier_missing_vat_is_visible_and_does_not_derive_gross_price(self):
        definition = MappingDefinition.model_validate({"bindings": [{"target": "code", "source": "id"},
            {"target": "name", "source": "title"}, {"target": "prices.purchase_net", "source": "net"}]})
        cfg = {"default_currency": "EUR", "adapter_settings": {"mapping": {"postprocess": {"product_code_prefix": "G-"}}}}
        mapped = mapping.apply_definition(None, {"id": "001", "title": "Tube", "net": "10"}, definition, "generic", "products", cfg)
        self.assertIsNone(mapped.prices.vat_percent)
        self.assertIsNone(mapped.prices.purchase_gross)
        self.assertEqual(mapped.prices.purchase_net, 10)
        self.assertIn("missing_vat", mapped.warnings)

    def test_shop_mapping_categories_are_shop_specific_and_preserve_raw_source(self):
        definition = MappingDefinition.model_validate({"category_rules": [{"source": "Tubes", "target_code": "leaf-10"}],
            "bindings": [{"target": "seo_title", "source": "title", "transforms": [{"op": "suffix", "value": " | BIKETREK"}]}]})
        source = product(category="Tubes")
        mapped = mapping.apply_definition(source, {"title": "Duša"}, definition, "paul-lange", "products", CFG, revision=4, shop="biketrek")
        self.assertEqual(mapped.target_category_code, "leaf-10")
        self.assertEqual(mapped.mapping_provenance["shop_revision"], 4)
        self.assertEqual(mapped.seo_title, "Duša | BIKETREK")
        self.assertEqual(source.target_category_code, None)
        with self.assertRaisesRegex(ValueError, "target shop"):
            mapping.validate_definition(definition)

    def test_generic_xml_nested_record_path_and_duplicate_identity(self):
        self.write('<root><items><item><id>001</id><name>One</name></item><item><id>001</id><name>Two</name></item></items></root>')
        definition = MappingDefinition.model_validate({"record_path": "root/items/item", "bindings": [
            {"target": "code", "source": "id"}, {"target": "name", "source": "name"}]})
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            mapping.parse_mapped(self.path, "sample", CFG, "products", definition, 1)

    def test_rejects_xml_external_entities_json_ambiguity_unsafe_destinations_and_headers(self):
        samples = ['<!DOCTYPE SHOP [<!ENTITY ext SYSTEM "file:///etc/passwd">]><SHOP><SHOPITEM><id>&ext;</id></SHOPITEM></SHOP>',
                   '{"one":[{"id":"a"}],"two":[{"id":"b"}]}', 'sku;sku\n1;2\n']
        for value in samples:
            self.write(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                read_records(self.path, MappingDefinition())
        for target in ("active_yn", "stock", "meta:validation_required", "eval"):
            with self.assertRaises(ValueError):
                mapping.validate_definition(MappingDefinition.model_validate({"bindings": [{"target": target, "constant": "1"}]}))

    def test_northfinder_native_variant_inheritance_and_blocked_rows_preserved(self):
        self.write("<products>" + northfinder_product(northfinder_variant(purchase="", retail="")) + "</products>")
        result, _ = mapping.parse_mapped(self.path, "northfinder", CFG, "products", MappingDefinition(), 1, parse_northfinder)[0]
        self.assertEqual(result.group_code, "N")
        self.assertEqual(str(result.prices.purchase_net), "10")
        self.assertIn("inherited_purchase_price", result.warnings)
        self.write("<products>" + northfinder_product(northfinder_variant() + northfinder_variant(ean="0002")) + "</products>")
        result, _ = mapping.parse_mapped(self.path, "northfinder", CFG, "products", MappingDefinition(), 2, parse_northfinder)[0]
        self.assertEqual(result.import_blockers, ["duplicate_supplier_code"])
        self.assertIsNone(result.prices.purchase_net)

    def test_unchanged_native_parameters_and_markup_do_not_get_rewritten(self):
        source = product(description='<p class="source">Text</p>')
        mapped = mapping.apply_definition(source, {}, MappingDefinition(), "paul-lange", "products", CFG)
        self.assertEqual(mapped.description, source.description)
        self.assertEqual(mapped.prices, source.prices)
        self.assertEqual(source.raw_fields, {})

    def test_stock_source_is_rejected_even_under_a_different_feed_key(self):
        from inventory_hub.services.catalog import CatalogError
        for key in ("stock", "availability"):
            with self.assertRaises(CatalogError):
                mapping._listing_scope({"feeds": {"sources": {"availability": {"type": "stock"}}}}, key)

    def test_xml_namespaces_are_preserved_and_inspected_paths_are_reusable(self):
        self.write('<products xmlns="https://supplier.example/feed"><product><id>01</id><params><param><name>Valve</name><value>SV</value></param></params></product></products>')
        records, kind, path = read_records(self.path, MappingDefinition())
        again, _, _ = read_records(self.path, MappingDefinition(record_path=path))
        self.assertEqual(records, again)
        report = inspect_records(records, kind, path)
        field = next(item for item in report["fields"] if item["path"].endswith("}value"))
        self.assertEqual(values_at(records[0]["fields"], field["path"]), ["SV"])

    def test_mixed_xml_text_and_markup_keep_tail_and_literal_pipe_headers_map(self):
        self.write('<items><item><id>01</id><name>Tube</name><description>Before <b>bold</b> after</description></item></items>')
        definition = MappingDefinition.model_validate({"record_path": "items/item", "bindings": [
            {"target": "code", "source": "id"}, {"target": "name", "source": "name"},
            {"target": "description", "source": "description/#inner_xml"},
            {"target": "short_description", "source": "description/#text"}]})
        # Explicit custom record path overrides the legacy Paul Lange XML layout.
        mapped, _ = mapping.parse_mapped(self.path, "paul-lange", CFG, "products", definition, 1, parse_catalog)[0]
        self.assertEqual(mapped.description, "Before <b>bold</b> after")
        self.assertEqual(mapped.short_description, "Before bold after")
        literal = MappingDefinition.model_validate({"bindings": [{"target": "parameter:Width", "source": "Size|Width"}]})
        mapped = mapping.apply_definition(product(), {"Size|Width": "50 mm"}, literal, "paul-lange", "products", CFG)
        self.assertEqual(mapped.parameters[-1].value, "50 mm")


if __name__ == "__main__":
    unittest.main()
