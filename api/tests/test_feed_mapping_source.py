"""Product collection discovery must not treat supplier taxonomy as products."""
import json
import tempfile
import unittest
from pathlib import Path

from catalog_fixtures import xml_item
from inventory_hub.adapters.paul_lange_catalog import parse_catalog
from inventory_hub.feed_mapping_types import MappingDefinition
from inventory_hub.services.feed_mapping import parse_mapped
from inventory_hub.services.feed_mapping_source import inspect_records, read_records, values_at


class FeedSourceDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "feed.xml"

    def inspect(self, source):
        self.path.write_text(source, encoding="utf-8")
        records, kind, path = read_records(self.path, MappingDefinition())
        return records, inspect_records(records, kind, path)

    def test_paul_lange_wrapper_taxonomy_and_native_record_path(self):
        category = "<CATEGORYID>00042</CATEGORYID><CATEGORYTEXT>Diely | Duše</CATEGORYTEXT>"
        records, report = self.inspect('<SHOP><SHOPITEMS version="2">' + xml_item(extra=category)
            + xml_item("A-002", extra=category) + '</SHOPITEMS><CATEGORIES><CATEGORY>'
            '<CATEGORY_ID>00042</CATEGORY_ID><CATEGORY_NAME>Duše</CATEGORY_NAME></CATEGORY></CATEGORIES></SHOP>')
        self.assertEqual(report["record_path"], "SHOP/SHOPITEMS/SHOPITEM")
        self.assertEqual(report["total_records"], 2)
        self.assertEqual(report["source_category_options"], [{"source": "00042", "code": "00042",
            "label": "Diely | Duše", "path": "Diely | Duše", "count": 2}])
        self.assertEqual(report["category_fields"], {"code_path": "CATEGORYID", "label_path": "CATEGORYTEXT"})
        self.assertEqual(report["source_categories"], ["00042", "Diely | Duše"])
        self.assertNotIn("CATEGORIES/CATEGORY/CATEGORY_NAME", {f["path"] for f in report["fields"]})
        cfg = {"adapter_settings": {"vat": 23, "mapping": {"postprocess": {"product_code_prefix": "PL-"}}}}
        mapped = parse_mapped(self.path, "paul-lange", cfg, "products",
            MappingDefinition(record_path=report["record_path"]), 1, parse_catalog)
        self.assertEqual(len(mapped), 2)
        self.assertEqual(mapped[0][0].code, "A-001")
        self.assertEqual(mapped[0][0].category_code, "00042")
        self.assertEqual(str(mapped[0][0].prices.purchase_net), "60")

    def test_metadata_beside_product_container_is_not_a_record(self):
        _, report = self.inspect('<feed><data version="2"><count>1</count><products><product>'
            '<id>01</id><name>Duša</name></product></products></data></feed>')
        self.assertEqual(report["record_path"], "feed/data/products/product")
        _, report = self.inspect('<feed><payload version="2"><count>1</count><rows><row>'
            '<id>01</id><name>Duša</name></row></rows></payload></feed>')
        self.assertEqual(report["record_path"], "feed/payload/rows/row")

    def test_xml_two_product_collections_are_not_silently_combined(self):
        self.path.write_text('<feed><current><product><id>01</id></product></current>'
            '<archived><product><id>02</id></product></archived></feed>', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "several possible record collections"):
            read_records(self.path, MappingDefinition())
        rows, _, _ = read_records(self.path, MappingDefinition(record_path="feed/current/product"))
        self.assertEqual(values_at(rows[0]["fields"], "id"), ["01"])

    def test_nested_json_products_with_metadata_and_categories(self):
        _, report = self.inspect(json.dumps({"data": {"total": 2, "products": [
            {"sku": "001", "name": "One", "category": {"id": "C1", "name": "Duše"}},
            {"sku": "002", "name": "Two", "category": {"id": "C1", "name": "Duše"}}]},
            "categories": [{"id": "C1", "name": "Duše"}]}))
        self.assertEqual(report["record_path"], "data/products")
        self.assertEqual(report["total_records"], 2)
        self.assertEqual(report["category_fields"], {"code_path": "category/id", "label_path": "category/name"})
        self.assertEqual(report["source_category_options"][0]["count"], 2)

    def test_empty_or_malformed_product_array_does_not_import_taxonomy(self):
        for products in ([], ["not a product"], [{"id": "1"}, 5]):
            self.path.write_text(json.dumps({"products": products,
                "categories": [{"id": "C1", "name": "Duše"}]}), encoding="utf-8")
            with self.subTest(products=products), self.assertRaisesRegex(ValueError, "No records|select objects"):
                read_records(self.path, MappingDefinition())
        self.path.write_text('<SHOP><SHOPITEMS/><CATEGORIES><CATEGORY><id>C1</id><name>Duše</name>'
            '</CATEGORY></CATEGORIES></SHOP>', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "No product records"):
            read_records(self.path, MappingDefinition())

    def test_parameter_discovery_pairs_values_in_the_same_object_and_counts_records(self):
        _, report = self.inspect('<SHOP>' + xml_item() + xml_item("A-002", extra=
            '<PARAMS><PARAM><NAME>Material</NAME><VALUE>Butyl</VALUE></PARAM></PARAMS>') + '</SHOP>')
        color = next(p for p in report["source_parameters"] if p["name"] == "Farba")
        self.assertEqual(color, {"name": "Farba", "source": "DYN_PARAMS/PARAM", "param_name_path": "DESC",
            "param_value_path": "VAL", "examples": ["červená"], "populated": 2, "total": 2})
        material = next(p for p in report["source_parameters"] if p["name"] == "Material")
        self.assertEqual(material["populated"], 1)
        _, report = self.inspect(json.dumps({"products": [{"id": "1", "props": [
            {"n": " Width ", "v": "50 mm"}, {"n": "Width", "v": "2 in"},
            {"n": ["Height", "Color"], "v": ["20", "red"]}]}]}))
        self.assertEqual(len(report["source_parameters"]), 1)
        self.assertEqual(report["source_parameters"][0]["name"], "Width")
        self.assertEqual(report["source_parameters"][0]["examples"], ["50 mm", "2 in"])
        self.assertEqual(report["source_parameters"][0]["populated"], 1)

    def test_namespaced_wrapper_discovery_paths_remain_usable(self):
        records, report = self.inspect('<feed xmlns="https://example.test/feed"><data><products><product>'
            '<id>01</id><name>Tube</name><category_code>007</category_code><category_name>Duše</category_name>'
            '<parameters><parameter><name>Valve</name><value>SV</value></parameter></parameters>'
            '</product></products></data></feed>')
        again, _, _ = read_records(self.path, MappingDefinition(record_path=report["record_path"]))
        self.assertEqual(records, again)
        parameter = report["source_parameters"][0]
        objects = values_at(records[0]["fields"], parameter["source"])
        self.assertEqual(values_at(objects[0], parameter["param_value_path"]), ["SV"])
        self.assertEqual(report["source_category_options"][0]["source"], "007")

    def test_northfinder_category_path_is_a_native_mapping_key(self):
        _, report = self.inspect('<products><product><id>1</id><name>Jacket</name>'
            '<categories_b2c><category>Oblečenie</category><category>Bundy</category></categories_b2c>'
            '<categories_b2b><category>Trade-only</category></categories_b2b></product></products>')
        combined = next(c for c in report["source_category_options"] if c["source"] == "Oblečenie | Bundy")
        self.assertEqual(combined["count"], 1)
        self.assertEqual(report["source_category_options"], [combined])
        self.assertEqual(report["category_fields"], {})
        self.assertIn("Trade-only", report["source_categories"])
        _, fallback = self.inspect('<products><product><id>1</id><name>Jacket</name>'
            '<categories_b2c/><categories_b2b><category>Trade</category><category>Jackets</category>'
            '</categories_b2b></product></products>')
        self.assertEqual(fallback["source_category_options"][0]["source"], "Trade | Jackets")


if __name__ == "__main__":
    unittest.main()
