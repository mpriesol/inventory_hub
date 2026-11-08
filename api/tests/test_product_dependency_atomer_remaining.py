"""Closed legacy spellings and availability conflicts emitted by real writers."""
from collections import defaultdict
from copy import deepcopy
from io import BytesIO
import unittest
from xml.sax.saxutils import escape

import test_product_dependency_atomer_scope as fixture
from inventory_hub.services import atomer_card_batch as cards, atomer_progress as progress
from inventory_hub.services import atomer_progress_store as store, product_dependency_scope as scope
from inventory_hub.services.product_identity import IdentityIndex, barcode_keys


CODE = fixture.CODE
OTHER_EAN = '4006381333931'
RAW_VALUES = ("'" + OTHER_EAN, OTHER_EAN + '.', '123456.789.01',
              'ABC-1234-567', 'AB0-1234', 'AB-1234CD-567xs', 'Jacket 13 XS čierna')


def report(raw='', *, actual=None, expected=None):
    parent = 'SYN-AVAILABILITY-PARENT' if expected is not None else ''
    _, _, sources = progress.parse_source(BytesIO((f'<PRODUCTS><PRODUCT><PRODUCT_ID>{CODE}</PRODUCT_ID>'
        f'<PARENT_ID>{parent}</PARENT_ID><PRODUCT_NAME>Synthetic unit</PRODUCT_NAME>'
        f'<EAN>{escape(raw)}</EAN></PRODUCT></PRODUCTS>').encode()))
    targets = []
    if expected is not None:
        native = {'product_id': 9001, 'code': parent, 'variants': [
            {'variant_id': 9002, 'code': CODE, 'ean': raw, 'availability': actual}]}
        targets = progress.flatten_catalog([native], [
            {'product_id': 9001, 'variant_id': 9002, 'code': CODE, 'ean': raw}])
    detail, = progress.reconcile(sources, targets, IdentityIndex(2), [], {}, defaultdict(list), {})
    if expected is not None:
        store.apply_availability_reviews([detail], targets, [(CODE, '9001', '9002', expected)])
    data = fixture.fixture(changed=False)
    data[0]['family_key'] = detail['family_key']
    data[1][CODE]['detail'] = detail
    return data


class AtomerRemainingScopeTests(unittest.TestCase):
    def test_real_source_writer_keeps_all_known_raw_spellings_and_rejects_publication(self):
        for raw in RAW_VALUES:
            with self.subTest(raw=raw):
                data = report(raw)
                self.assertEqual(data[1][CODE]['detail']['ean'], raw)
                with self.assertRaisesRegex(cards.CardConflict, 'atomer_source_ean_unverified'):
                    cards.barcode_subject(data[1][CODE]['detail'])
                before = deepcopy(data)
                result = fixture.inspect(data, barcodes=barcode_keys([OTHER_EAN]))
                self.assertEqual(result['classification'], 'context_only')
                self.assertEqual(result['matched_paths'], [])
                self.assertEqual(data, before)
                for namespace in ('aliases', 'skus'):
                    self.assertEqual(fixture.inspect(data, **{namespace: {raw.casefold()}})['classification'], 'reference')

    def test_whole_literal_and_each_word_or_compound_member_still_match(self):
        data = report('Jacket 13 XS čierna')
        for alias in ('jacket 13 xs čierna', 'jacket', '13', 'xs', 'čierna'):
            with self.subTest(alias=alias):
                result = fixture.inspect(data, aliases={alias})
                self.assertEqual(result['classification'], 'reference')
                self.assertIn(alias, {item['value'].casefold() for item in result['matched_paths']})
        for raw in RAW_VALUES:
            data = report(f'{raw}/{OTHER_EAN};{fixture.EAN}')
            for ean in (OTHER_EAN, fixture.EAN):
                result = fixture.inspect(data, barcodes=barcode_keys([ean]))
                self.assertEqual(result['classification'], 'reference')
                self.assertIn(ean, {item['value'] for item in result['matched_paths'] if item['namespace'] == 'barcode'})

    def test_raw_source_target_receipt_and_correction_before_use_same_closed_contract(self):
        for location in ('source', 'target', 'receipt', 'correction'):
            with self.subTest(location=location):
                raw = 'ABC-1234-567'
                data = fixture.fixture(changed=False)
                if location in ('source', 'target'):
                    data[1][CODE]['detail']['ean' if location == 'source' else 'target_ean'] = raw
                elif location == 'receipt':
                    data[0]['receipt']['cards'] = {CODE: {'state': 'blocked', 'evidence': {'ean': raw}}}
                else:
                    data[2]['selection'] = {'barcode_corrections': {CODE: {'before': raw, 'ean': OTHER_EAN}}}
                self.assertEqual(fixture.inspect(data, aliases={raw.casefold()})['classification'], 'reference')

    def test_no_other_punctuation_numeric_coercion_or_hidden_compound_is_accepted(self):
        malformed = [OTHER_EAN + '.0', "''" + OTHER_EAN, OTHER_EAN + "'", '\u2019' + OTHER_EAN,
            OTHER_EAN + '..', OTHER_EAN + '.' + fixture.EAN, '123456.789.012',
            OTHER_EAN + '|' + fixture.EAN, OTHER_EAN + '-' + fixture.EAN,
            'ABC-' + OTHER_EAN, 'AB-1234CD-' + OTHER_EAN, 'čierna' + OTHER_EAN,
            '\u0661\u0662\u0663', 'Jacket (13) XS čierna', 'Jacket 13 XS čierna!',
            '\u200b' + OTHER_EAN, False, 0, [], {}, {'ean': OTHER_EAN}]
        for raw in malformed:
            with self.subTest(raw=raw):
                data = fixture.fixture(changed=False)
                data[1][CODE]['detail']['ean'] = raw
                with self.assertRaises(scope.Unverified):
                    fixture.inspect(data)

    def test_real_availability_writer_keeps_conflict_and_exact_native_parent_scope(self):
        for actual in ('Available', None):
            with self.subTest(actual=actual):
                data = report(OTHER_EAN, actual=actual, expected='Pending')
                detail = data[1][CODE]['detail']
                self.assertEqual(detail['state'], 'review')
                self.assertIn('availability_differs', detail['reasons'])
                self.assertEqual(detail['availability_review'], {'atomer': 'Pending', 'xtrek': actual})
                before = deepcopy(data)
                result = fixture.inspect(data)
                self.assertEqual(result['classification'], 'context_only')
                self.assertIn('recorded_atomer_availability_conflict', {item['reason'] for item in result['context_paths']})
                for selected in ({'native': {('xtrek', 9001, 9002)}},
                                 {'native_parents': {('xtrek', 9001)}},
                                 {'parents': {('xtrek', 'syn-availability-parent')}}):
                    self.assertEqual(fixture.inspect(data, **selected)['classification'], 'reference')
                self.assertEqual(data, before)

    def test_availability_labels_are_context_even_if_they_resemble_an_identity(self):
        data = report(OTHER_EAN, actual=fixture.EAN, expected='SYN-OLD')
        self.assertEqual(fixture.inspect(data)['classification'], 'context_only')
        data[1][CODE]['detail']['hub_id'] = 2
        self.assertEqual(fixture.inspect(data)['classification'], 'reference')

    def test_availability_requires_exact_shape_lifecycle_and_variant_identity(self):
        mutations = [
            {'state': 'linked'}, {'kind': 'standalone'}, {'reasons': []},
            {'reasons': ['kind_differs']}, {'reasons': ['availability_differs', {}]},
            {'target_code': 'OTHER'}, {'product_id': None}, {'product_id': False},
            {'variant_id': None}, {'variant_id': True},
            {'availability_review': None}, {'availability_review': {}},
            {'availability_review': {'atomer': 'Pending'}},
            {'availability_review': {'atomer': 'Pending', 'xtrek': 'pending'}},
            {'availability_review': {'atomer': '', 'xtrek': 'Available'}},
            {'availability_review': {'atomer': None, 'xtrek': 'Available'}},
            {'availability_review': {'atomer': 'Pending', 'xtrek': False}},
            {'availability_review': {'atomer': 'Pending', 'xtrek': {'product_id': 2}}},
            {'availability_review': {'atomer': 'Pending', 'xtrek': 'Available', 'future_id': 2}},
        ]
        for change in mutations:
            with self.subTest(change=change):
                data = report(OTHER_EAN, actual='Available', expected='Pending')
                data[1][CODE]['detail'].update(change)
                with self.assertRaises(scope.Unverified):
                    fixture.inspect(data)


if __name__ == '__main__':
    unittest.main()
