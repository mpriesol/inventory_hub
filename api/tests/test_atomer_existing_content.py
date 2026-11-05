from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from inventory_hub.services import atomer_existing_content as content
from inventory_hub.services import atomer_new_native as native
from inventory_hub.services.atomer_generations import parameters
from test_atomer_new_native import creation, live


class ExistingContentTests(unittest.TestCase):
    def setUp(self):
        self.doc = creation()
        self.actual = live(self.doc)
        self.actual['stock'] = 15
        self.actual['stocks'] = [dict(stock_id=1, quantity=15)]
        self.actual['descriptions'][0]['url'] = 'https://shop.example/product'
        self.actual['descriptions'].append(dict(language='cs', title='Ručný text'))
        self.actual['labels'].append(dict(label_id=66, active_yn=True, active_currently_yn=True))
        self.doc['managed_label_ids'] = [21, 22]
        self.actual['variants'][0]['labels'] = []
        self.ids = dict(product_id=101, product_code='BAG', variant_ids={'BLUE': 202},
                        variant_codes={'BLUE': 'BLUE'}, allow_parent_write=True)

    def prepare(self, queue=()):
        with patch.object(native, '_connection'), patch.object(native, '_read', return_value={'BAG': deepcopy(self.actual)}), \
             patch.object(content, '_download_queue', return_value=queue):
            return content.prepare_content([self.doc], 'fp', {'BAG': self.ids})['BAG']

    def observe(self, plan):
        with patch.object(native, '_connection'), patch.object(native, '_read', return_value={'BAG': deepcopy(self.actual)}):
            return content.observe_content([self.doc], 'fp', {'BAG': plan})['BAG']

    def send(self, plan):
        with patch.object(native, '_connection'), patch.object(native, '_read', return_value={'BAG': deepcopy(self.actual)}), \
             patch.object(content, 'update_products', return_value='') as write:
            result = content.send_content({'BAG': plan}, 'fp')['BAG']
        return result, write

    def test_unchanged_legacy_family_is_verified_including_current_protected_links(self):
        self.actual['related'].append('MANUAL')
        self.actual['categories'].append(dict(category_id=90, main_yn=False, position=4))
        self.actual['parameters_new'] = parameters({'Pôvodný parameter': 'OK'})
        r = self.prepare()
        self.assertEqual(r['status'], 'verified')
        self.assertTrue(r['verified_fields'])
        self.assertEqual(r['variants'], {'BLUE': 202})
        self.assertTrue(r['identity_verified'])

    def test_native_aliases_are_pinned_without_rewriting_either_sku_or_missing_source_gtin(self):
        self.actual['code'] = self.ids['product_code'] = 'ORIGINAL'
        leaf = self.actual['variants'][0]
        leaf.update(code='BLUE-ORIGINAL', product_code='ORIGINAL')
        self.ids['variant_codes']['BLUE'] = 'BLUE-ORIGINAL'
        self.doc['payload']['variants'][0].pop('ean')
        self.doc['payload']['variants'][0]['weight'] = 555
        plan = self.prepare()
        self.assertEqual(plan['payload'], dict(code='ORIGINAL', variants=[dict(code='BLUE-ORIGINAL', weight=555)]))
        result, write = self.send(plan)
        self.assertEqual(result['status'], 'sent')
        write.assert_called_once_with([plan['payload']], 'fp')
        leaf['weight'] = 555
        result = self.observe(plan)
        self.assertEqual(result['status'], 'verified')
        self.assertTrue(result['tranche_verified'])
        # The exact barcode belongs to the existing native leaf, even when the source column is empty.
        leaf['ean'] = ''
        self.assertEqual(self.observe(plan)['status'], 'conflict')

    def test_existing_valid_different_gtin_is_a_conflict(self):
        self.actual['variants'][0]['ean'] = '5901234123457'
        plan = self.prepare()
        self.assertEqual(plan['status'], 'conflict')
        self.assertFalse(plan['identity_verified'])
        self.assertNotIn('payload', plan)

    def test_fresh_before_and_protected_after_block_stock_or_other_language_edits(self):
        self.actual['descriptions'][0]['long_description'] = '<p>Old</p>'
        plan = self.prepare()
        self.assertEqual(plan['payload'], {'code': 'BAG', 'descriptions': [
            dict(language='sk', long_description='<p><strong>Opis</strong></p>')]})
        self.actual['stock'] = 16
        result, write = self.send(plan)
        self.assertEqual(result['status'], 'conflict')
        write.assert_not_called()
        self.actual['stock'] = 15
        self.actual['descriptions'][0]['long_description'] = self.doc['payload']['descriptions'][0]['long_description']
        self.assertEqual(self.observe(plan)['status'], 'verified')
        self.actual['descriptions'][1]['title'] = 'Modified'
        self.assertEqual(self.observe(plan)['status'], 'conflict')

    def test_manual_or_extra_leaf_prevents_parent_edits_but_scoped_leaf_can_be_corrected(self):
        extra = deepcopy(self.actual['variants'][0])
        extra.update(code='LEGACY', variant_id=999, main_yn=False, parameters_new=parameters({'Farba': 'Snow'}))
        self.actual['variants'].append(extra)
        self.actual['descriptions'][0]['title'] = 'Pôvodné'
        self.doc['payload']['variants'][0]['active_yn'] = False
        plan = self.prepare()
        self.assertEqual(plan['payload'], dict(code='BAG', variants=[dict(code='BLUE', active_yn=False)]))
        self.actual['variants'][0]['active_yn'] = False
        result = self.observe(plan)
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['tranche_verified'])
        self.assertTrue(any('chránené' in msg for msg in result['issues']))
        self.actual['variants'][-1]['active_yn'] = False
        self.assertEqual(self.observe(plan)['status'], 'conflict')

    def test_occupied_combination_cannot_be_assigned_to_another_exact_native_id(self):
        extra = deepcopy(self.actual['variants'][0])
        extra.update(code='LEGACY', variant_id=999, main_yn=False, parameters_new=parameters({'Farba': 'Snow'}))
        self.actual['variants'].append(extra)
        self.doc['payload']['variants'][0]['parameters'] = parameters({'Farba': 'Snow'})
        plan = self.prepare()
        self.assertEqual(plan['status'], 'partial')
        self.assertIn('existing_content_axes_occupied', plan['issues'])
        self.assertEqual(plan['payload'], {'code': 'BAG'})

    def test_scoped_flags_and_category_changes_preserve_native_extras(self):
        original = self.actual['labels'][0].copy()
        self.doc['payload']['labels'] = [dict(label_id=21, active_yn=True)]
        self.doc['flags']['BAG'] = ['Akcia']
        self.doc['payload']['variants'][0]['labels'] = [dict(label_id=21, active_yn=False)]
        self.actual['categories'] = [dict(category_id=44, main_yn=True, position=8)]
        plan = self.prepare()
        patch_row = plan['payload']
        self.assertEqual(set(x['category_id'] for x in patch_row['categories']), {44, 3})
        self.assertFalse(next(c for c in patch_row['categories'] if c['category_id'] == 44)['main_yn'])
        self.assertEqual({v['label_id'] for v in patch_row['labels']}, {66, 21})
        self.assertFalse(patch_row['variants'][0]['labels'][0]['active_yn'])
        self.actual['categories'] = patch_row['categories']
        self.actual['labels'] = [original, dict(label_id=21, active_yn=True, active_currently_yn=True)]
        self.actual['variants'][0]['labels'] = [dict(label_id=21, active_yn=False, active_currently_yn=False)]
        self.assertEqual(self.observe(plan)['status'], 'verified')
        self.actual['labels'].pop(0)
        self.assertEqual(self.observe(plan)['status'], 'conflict')

    def test_gallery_backfill_requires_all_original_ids_and_source_image_readback(self):
        wanted = self.doc['payload']['images']
        second = dict(url='https://www.xtrek.sk/user/22/upload/photo2.jpg', main_yn=False, list_yn=False, position=2)
        wanted.append(second)
        plan = self.prepare()
        self.assertEqual(plan['payload']['images'][0]['file_id'], 33)
        self.assertEqual(plan['payload']['images'][1], second)
        result = self.observe(plan)
        self.assertEqual(result['status'], 'partial')
        self.assertIn('images', result['deferred'])
        self.actual['images'].append(dict(second, file_id=35, url='https://cdn.example/x-photo2.jpg'))
        self.assertEqual(self.observe(plan)['status'], 'verified')
        self.actual['images'][0]['file_id'] = 46
        self.assertEqual(self.observe(plan)['status'], 'conflict')

    def test_retail_repair_preserves_other_pricelists_and_their_terms(self):
        groups = self.actual['variants'][0]['prices']
        groups[0]['pricelists'].append(dict(name='Velkoobchod', price_original=8, price_with_vat=8))
        retail = groups[0]['pricelists'][0]
        retail.update(price_original=12, price_with_vat=12)
        plan = self.prepare()
        self.assertEqual(plan['payload']['variants'][0]['prices'], self.doc['payload']['variants'][0]['prices'])
        retail.update(price_original=13, price_with_vat=13)
        self.assertEqual(self.observe(plan)['status'], 'verified')
        groups[0]['pricelists'][1]['price_original'] = 9
        self.assertEqual(self.observe(plan)['status'], 'conflict')

    def test_same_photo_already_in_provider_queue_is_not_resubmitted(self):
        uri = 'https://www.xtrek.sk/user/22/upload/photo2.jpg'
        self.doc['payload']['images'].append(dict(url=uri, main_yn=False, list_yn=False, position=2))
        queued = [dict(product_code='BAG', product_id=101, variant_code=None, url=uri)]
        plan = self.prepare(queued)
        self.assertEqual(plan['status'], 'partial')
        self.assertTrue(plan['gallery_wait'])
        self.assertNotIn('images', plan['payload'])
        fresh = self.prepare()
        with patch.object(native, '_connection'), patch.object(native, '_read', return_value={'BAG': self.actual}), \
             patch.object(content, '_download_queue', return_value=queued), patch.object(content, 'update_products') as put:
            self.assertEqual(content.send_content({'BAG': fresh}, 'fp')['BAG']['status'], 'conflict')
            put.assert_not_called()

    def test_an_existing_sale_does_not_get_a_different_base_price(self):
        self.actual['variants'][0]['prices'][0]['pricelists'][0].update(price_sale=9, price_with_vat=9)
        plan = self.prepare()
        self.assertEqual(plan['status'], 'partial')
        self.assertNotIn('variants', plan['payload'])
        self.assertIn('existujúca zľava', ' '.join(plan['issues']))

    def test_large_family_confirms_applied_tranche_before_starting_a_new_one(self):
        root = self.doc['payload']
        for i in range(104):
            sku = 'LEAF-' + str(i)
            axis = parameters({'Farba': str(i)})
            root['variants'].append(dict(root['variants'][0], code=sku, ean=None, parameters=axis, weight=111, main_yn=False))
            leaf = dict(self.actual['variants'][0], code=sku, variant_id=300+i, ean=None, weight=200,
                        parameters_new=axis, main_yn=False)
            self.actual['variants'].append(leaf)
            self.doc['availability'][sku] = '3 dni'
            self.ids['variant_ids'][sku] = 300+i
            self.ids['variant_codes'][sku] = sku
        plan = self.prepare()
        self.assertEqual(len(plan['payload']['variants']), 100)
        first = {v['code'] for v in plan['payload']['variants']}
        self.assertFalse(self.observe(plan)['tranche_verified'])
        for leaf in self.actual['variants']:
            if leaf['code'] in first:
                leaf['weight'] = 111
        next_result = self.observe(plan)
        self.assertTrue(next_result['tranche_verified'])
        self.assertTrue(next_result['remaining_content'])
        self.assertEqual(next_result['status'], 'partial')
        self.assertIn('content_tranche', next_result['deferred'])
        self.assertEqual(len(next_result['remaining_fields']), 4)
