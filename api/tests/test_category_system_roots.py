"""System menu roots must not become product category assignments."""
import unittest

from inventory_hub.services.catalog import CatalogError
from inventory_hub.services.catalog_merchandising import category_chain, category_rows, system_category_codes


class CategorySystemRootTests(unittest.TestCase):
    def raw_tree(self):
        # Names, IDs and codes deliberately differ from the production shop.
        return [
            {'category_id': 42, 'code': 'navigation', 'parent_id': None, 'parent_code': None,
             'type': 'site', 'descriptions': [{'language': 'sk', 'name': 'Navigácia'}]},
            {'category_id': 53, 'code': 'real-root', 'parent_id': 42, 'type': 'siteWithProducts'},
            {'category_id': 67, 'code': 'parent', 'parent_id': 53, 'type': 'siteWithProducts'},
            {'category_id': 79, 'code': 'leaf', 'parent_id': 67, 'type': 'siteWithProducts'},
        ]

    def test_navigation_is_retained_but_product_chain_excludes_system_root(self):
        rows = category_rows(self.raw_tree())
        self.assertEqual(len(rows), 4)
        self.assertFalse(rows[0]['assignable'])
        self.assertTrue(rows[0]['system_root'])
        self.assertEqual(rows[1]['parent_code'], 'navigation')
        self.assertTrue(all(row['assignable'] for row in rows[1:]))
        self.assertEqual(category_chain(rows, 'leaf'), [
            {'code': 'real-root', 'main_yn': False}, {'code': 'parent', 'main_yn': False},
            {'code': 'leaf', 'main_yn': True},
        ])

    def test_system_root_is_not_a_selectable_primary_category(self):
        with self.assertRaises(CatalogError) as error:
            category_chain(category_rows(self.raw_tree()), 'navigation')
        self.assertEqual(error.exception.code, 'category_not_assignable')

    def test_historical_readback_allowlist_uses_only_explicit_system_metadata(self):
        raw = self.raw_tree()
        self.assertEqual(system_category_codes(raw), {'navigation'})
        self.assertEqual(system_category_codes(category_rows(raw)), {'navigation'})
        self.assertEqual(system_category_codes([
            {'category_id': 42, 'code': 'navigation', 'parent_code': None, 'names': {'sk': 'Left menu'}},
            {'category_id': 3, 'code': 'real-root', 'parent_code': None, 'type': 'site'},
        ]), set())

    def test_missing_or_zero_parent_id_is_not_explicit_null_system_metadata(self):
        for root in ({'category_id': 42, 'code': 'root'},
                     {'category_id': 42, 'code': 'root', 'parent_id': 0}):
            rows = category_rows([root, {'category_id': 53, 'code': 'leaf', 'parent_id': 42}])
            self.assertTrue(rows[0]['assignable'])
            self.assertEqual(category_chain(rows, 'leaf'), [
                {'code': 'root', 'main_yn': False}, {'code': 'leaf', 'main_yn': True}])

    def test_static_content_type_alone_never_removes_a_real_ancestor(self):
        raw = self.raw_tree()
        raw[1]['type'] = 'site'
        rows = category_rows(raw)
        self.assertTrue(rows[1]['assignable'])
        self.assertEqual([row['code'] for row in category_chain(rows, 'leaf')], ['real-root', 'parent', 'leaf'])

    def test_missing_real_ancestor_and_cycle_still_block_assignment(self):
        for invalid in ([{'code': 'leaf', 'parent_code': 'missing'}],
                        [{'code': 'leaf', 'parent_code': 'parent'}, {'code': 'parent', 'parent_code': 'leaf'}]):
            with self.assertRaises(CatalogError):
                category_chain(invalid, 'leaf')


if __name__ == '__main__':
    unittest.main()
