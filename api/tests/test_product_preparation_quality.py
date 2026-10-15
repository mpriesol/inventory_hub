"""Regressions for fixed SKUs, concise copy and nonduplicated specifications."""
import unittest

from catalog_fixtures import product
from inventory_hub.catalog_types import CatalogParameter, ShopImportOptions
from inventory_hub.services.ai_content_editorial import normalize_generated_content
from inventory_hub.services.catalog_variants import normalize_singleton_groups
from inventory_hub.services.catalog_import import build_item
from test_ai_content import content


class ProductPreparationQualityTests(unittest.TestCase):
    def test_single_sku_attributes_become_parameters_without_changing_identity(self):
        item = product(group_code='helmet', group_name='Helmet', variant_relationship='explicit',
                       variant_attributes=[CatalogParameter(name='Veľkosť', value='M')])
        normalize_singleton_groups([item])
        payload = build_item([item], ShopImportOptions(), {}, True).payload
        self.assertNotIn('variants', payload)
        self.assertEqual(payload['code'], item.shop_code)
        self.assertEqual(payload['ean'], item.eans[0])
        self.assertEqual(payload['parameters'][0]['values'][0]['descriptions'][0]['value'], 'M')
        self.assertIsNone(item.group_code)

    def test_one_selected_variant_keeps_real_full_feed_family_without_main_variant(self):
        items = [product(id=i, group_code='helmet', variant_relationship='explicit',
            variant_attributes=[CatalogParameter(name='Veľkosť', value=size)]) for i, size in [(1, 'M'), (2, 'L')]]
        normalize_singleton_groups(items[:1], {('paul-lange', 'products', 'helmet'): 2})
        self.assertEqual(items[0].group_code, 'helmet')
        for selected in [items, items[:1]]:
            result = build_item(selected, ShopImportOptions(), {}, True)
            self.assertEqual(result.status, 'ready')
            self.assertTrue(all(v['main_yn'] is False for v in result.payload['variants']))
            self.assertEqual([v['code'] for v in result.payload['variants']], [p.shop_code for p in selected])

    def test_short_titles_remove_known_colour_and_clothing_size_only(self):
        context = {'prompt_version': 4, 'facts': [{'parameters': [
            {'name': 'Farba', 'value': 'modrá'}, {'name': 'Veľkosť', 'value': 'M'},
            {'name': 'Veľkosť', 'value': 'L'}, {'name': 'Veľkosť', 'value': '700x40C'}]}]}
        for title, expected in [
            ('BBB BHE-35 Condor - cyklistická prilba M', 'BBB BHE-35 Condor - cyklistická prilba'),
            ('BBB BSM-11 Guardian - puzdro na telefón na bicykel, veľkosť L', 'BBB BSM-11 Guardian - puzdro na telefón na bicykel'),
            ('BBB BHE-153 Hawk Mips - cestná prilba M, modrá', 'BBB BHE-153 Hawk Mips - cestná prilba'),
            ('BBB BSM-11 Guardian - puzdro vo veľkosti L', 'BBB BSM-11 Guardian - puzdro'),
            ('Schwalbe - plášť 700x40C', 'Schwalbe - plášť 700x40C'),
            ('Model M - prilba', 'Model M - prilba'),
            ('Shimano - kazeta 11-rýchlostná', 'Shimano - kazeta 11-rýchlostná'),
        ]:
            with self.subTest(title=title):
                self.assertEqual(normalize_generated_content(content(title=title).model_dump(), context)['title'], expected)

    def test_only_fully_duplicated_parameter_table_is_removed(self):
        params = [{'name': 'Materiál', 'values': ['Nylon'], 'product_id': None}]
        duplicated = '<h2>Technické parametre</h2><table><tr><th>Parameter</th><th>Hodnota</th></tr><tr><td>Materiál</td><td>Nylon</td></tr></table>'
        body = '<p>Odolné puzdro.</p>' + duplicated
        raw = content(long_description=body, parameters=params).model_dump()
        output = normalize_generated_content(raw, {'prompt_version': 4})
        self.assertEqual(output['long_description'], '<p>Odolné puzdro.</p>')
        self.assertEqual(output['parameters'], params)
        self.assertEqual(raw['long_description'], body, 'Original output is not mutated')
        for html in [body.replace('Nylon</td>', 'Nylon + iná informácia</td>'),
                     body.replace('Technické parametre', 'Kompatibilita'),
                     '<table><tr><td>ETRTO</td><td>Palce</td><td>Ventil</td></tr><tr><td>40-622</td><td>28</td><td>AV</td></tr></table>',
                     body.replace('Nylon</td>', '<a href="https://example.test/spec">Nylon</a></td>')]:
            self.assertEqual(normalize_generated_content({**raw, 'long_description': html}, {'prompt_version': 4})['long_description'], html)
        self.assertIs(normalize_generated_content(raw, {'prompt_version': 3}), raw)
        unsafe = {**raw, 'long_description': body.replace('<table>', '<table onclick="alert(1)">')}
        self.assertIs(normalize_generated_content(unsafe, {'prompt_version': 4}), unsafe)


if __name__ == '__main__':
    unittest.main()
