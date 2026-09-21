import unittest

from catalog_fixtures import product
from inventory_hub.catalog_types import CatalogParameter, ShopImportOptions
from inventory_hub.services.catalog_import import build_item
from inventory_hub.services.catalog_sort import variant_sort_key


class VariantSortTests(unittest.TestCase):
    def test_colours_then_apparel_aliases_and_numeric_sizes_without_changing_values(self):
        sizes = ["XXXL", "L", "XS", "2XL", "S", "XL", "M", "XXL"]
        variants = [product(i + 1, group_code="G1", variant_relationship="explicit", variant_attributes=[
            CatalogParameter(name="FARBA", value=color), CatalogParameter(name="VEĽKOSŤ", value=size)])
            for i, (color, size) in enumerate([(color, size) for color in ("red", "blue") for size in sizes])]
        before = [p.model_dump() for p in variants]
        ordered = sorted(variants, key=variant_sort_key)
        self.assertEqual([p.variant_attributes[0].value for p in ordered], ["blue"] * 8 + ["red"] * 8)
        self.assertEqual([p.variant_attributes[1].value for p in ordered[:8]], ["XS", "S", "M", "L", "XL", "2XL", "XXL", "XXXL"])
        item = build_item(variants, ShopImportOptions(), {}, True)
        self.assertEqual(item.product_ids, [p.id for p in ordered])
        self.assertEqual([p.model_dump() for p in variants], before)
        numeric = [product(i + 1, variant_attributes=[CatalogParameter(name="Size", value=size)]) for i, size in enumerate(["100", "40", "9", "42"])]
        self.assertEqual([p.variant_attributes[0].value for p in sorted(numeric, key=variant_sort_key)], ["9", "40", "42", "100"])
