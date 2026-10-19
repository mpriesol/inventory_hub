"""Identity and image-scope regressions; all data is synthetic."""
import unittest
from inventory_hub.services.catalog_shop_images import image_urls, verified_images
from inventory_hub.config_normalize import normalize_supplier_config


def facts(**changes):
    return {'source_ean': '5901234123457', 'external_id': '22', 'is_variant': True,
        'sku': 'SHOP-S', 'variant_code': 'SHOP-S', 'external_code': 'PARENT', 'parent_code': 'PARENT',
        'cached_parent_code': 'PARENT', 'parent_id': '11', 'leaf_code': 'SHOP-S', 'leaf_parent_id': '11',
        'variant_id': '22', 'leaf_ean': '5901234123457', 'leaf_count': 1,
        'images': [{'url': 'https://images.example.test/own.jpg'}], 'image': None,
        'parent_images': [{'url': 'https://images.example.test/parent.jpg'}], **changes}


class CatalogShopImageTests(unittest.TestCase):
    def test_exact_leaf_images_precede_explicit_parent_inheritance(self):
        self.assertEqual(verified_images(facts()), ['https://images.example.test/own.jpg'])
        self.assertEqual(verified_images(facts(images=[], image={'url': 'https://images.example.test/single.jpg'})),
                         ['https://images.example.test/single.jpg'])
        self.assertEqual(verified_images(facts(images=[])), ['https://images.example.test/parent.jpg'])

    def test_wrong_identity_duplicate_leaves_or_barcode_conflict_are_rejected(self):
        for changes in [{'leaf_count': 2}, {'leaf_code': 'OTHER'}, {'sku': 'OTHER'}, {'variant_id': '99'},
                        {'parent_id': None}, {'leaf_parent_id': '99'}, {'cached_parent_code': 'OTHER'},
                        {'external_id': None}, {'leaf_ean': '4006381333931'}]:
            with self.subTest(changes=changes):
                self.assertEqual(verified_images(facts(**changes)), [])

    def test_unsafe_images_are_skipped_and_main_is_first(self):
        values = ['http://images.example.test/no.jpg', 'https://user:secret@example.test/no.jpg',
                  'javascript:alert(1)', 'https://images.example.test/second.jpg',
                  {'url': 'https://images.example.test/main.jpg', 'main_yn': True},
                  'https://images.example.test/second.jpg', 'https://images.example.test/\nno.jpg']
        self.assertEqual(image_urls(values), ['https://images.example.test/main.jpg', 'https://images.example.test/second.jpg'])

    def test_explicit_shop_and_brand_policy_is_validated(self):
        cfg = normalize_supplier_config({'adapter_settings': {'catalog_image_fallback': {
            'enabled': True, 'shop_code': 'biketrek', 'brands': ['Topeak', 'sensor']}}})
        self.assertEqual(cfg['adapter_settings']['catalog_image_fallback']['brands'], ['SENSOR', 'TOPEAK'])
        with self.assertRaises(ValueError):
            normalize_supplier_config({'adapter_settings': {'catalog_image_fallback': {'enabled': True}}})
