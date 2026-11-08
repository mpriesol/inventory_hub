"""Exact Atomer report identities, including unresolved historical values."""
from collections import defaultdict
from copy import deepcopy
from io import BytesIO
import unittest

import test_product_dependency_atomer_scope as fixture
from inventory_hub.services import atomer_card_batch as cards, atomer_progress as progress
from inventory_hub.services import product_dependency_scope as scope, product_link_source as source
from inventory_hub.services.product_identity import IdentityIndex, RemoteIdentity, barcode_keys


CODE, EAN = fixture.CODE, fixture.EAN


def kind_conflict(*, target_variant=False):
    """Use the real source/report writers, not a fabricated successful mapping."""
    source_parent = '' if target_variant else 'SYN-SOURCE-PARENT'
    _, _, rows = progress.parse_source(BytesIO((f'<PRODUCTS><PRODUCT><PRODUCT_ID>{CODE}</PRODUCT_ID>'
        f'<PARENT_ID>{source_parent}</PARENT_ID><PRODUCT_NAME>Synthetic unit</PRODUCT_NAME>'
        '<EAN></EAN></PRODUCT></PRODUCTS>').encode()))
    native = {'product_id': 9001, 'code': 'SYN-NATIVE-PARENT' if target_variant else CODE}
    variants = []
    if target_variant:
        native['variants'] = [{'variant_id': 9002, 'code': CODE}]
        variants = [{'product_id': 9001, 'variant_id': 9002, 'code': CODE}]
    targets = progress.flatten_catalog([native], variants)
    detail, = progress.reconcile(rows, targets, IdentityIndex(2), [], {}, defaultdict(list), {})
    data = fixture.fixture(changed=False)
    data[0]['family_key'] = detail['family_key']
    data[1][CODE]['detail'] = detail
    return data


class AtomerLegacyScopeTests(unittest.TestCase):
    def test_actual_report_preserves_invalid_checksum_without_promoting_it(self):
        raw = '4006381333932'
        _, _, rows = progress.parse_source(BytesIO((f'<PRODUCTS><PRODUCT><PRODUCT_ID>{CODE}</PRODUCT_ID>'
            f'<EAN>{raw}</EAN></PRODUCT></PRODUCTS>').encode()))
        detail, = progress.reconcile(rows, {}, IdentityIndex(2), [], {}, defaultdict(list), {})
        self.assertEqual(detail['ean'], raw)
        with self.assertRaisesRegex(cards.CardConflict, 'atomer_source_ean_unverified'):
            cards.barcode_subject(detail)
        data = fixture.fixture(changed=False)
        data[0]['family_key'] = detail['family_key']
        data[1][CODE]['detail'] = detail
        before = deepcopy(data)
        result = fixture.inspect(data)
        self.assertEqual(result['classification'], 'context_only')
        self.assertEqual(data, before)
        self.assertEqual(fixture.inspect(data, aliases={raw})['classification'], 'reference')

    def test_compounds_inspect_every_valid_member_and_exact_raw_alias(self):
        other = '4006381333931'
        for raw in (f'RAW123R/{EAN};{other}', f'{other}, {EAN}', f'{other}\t{EAN}',
                    f'{other}\n{EAN}', f'0000/{other}/{EAN}'):
            for selected in (EAN, other):
                with self.subTest(raw=raw, selected=selected):
                    data = fixture.fixture(changed=False)
                    data[1][CODE]['detail']['ean'] = raw
                    result = fixture.inspect(data, barcodes=barcode_keys([selected]))
                    self.assertEqual(result['classification'], 'reference')
                    self.assertIn('barcode', {m['namespace'] for m in result['matched_paths']})
            self.assertEqual(fixture.inspect(data, barcodes=set(), aliases={raw.casefold()})['classification'], 'reference')
        data = fixture.fixture(changed=False)
        data[1][CODE]['detail']['ean'] = 'RAW123R/OTHER9'
        self.assertEqual(fixture.inspect(data, aliases={'raw123r'})['classification'], 'reference')

    def test_embedded_numeric_substring_is_not_a_gtin_and_repeated_sku_stays_raw(self):
        data = fixture.fixture(changed=False)
        data[1][CODE]['detail']['ean'] = EAN + 'R'
        self.assertEqual(fixture.inspect(data)['classification'], 'context_only')
        self.assertEqual(fixture.inspect(data, aliases={(EAN + 'R').casefold()})['classification'], 'reference')
        data[1][CODE]['detail']['ean'] = CODE
        self.assertEqual(fixture.inspect(data)['classification'], 'context_only')
        self.assertEqual(fixture.inspect(data, aliases={CODE.casefold()})['classification'], 'reference')

    def test_malformed_raw_values_or_unrecognized_separators_keep_hold(self):
        for raw in (False, 0, 123, [], {}, [EAN], ' ', EAN + '|4006381333931',
                    EAN + ':4006381333931', EAN + '-4006381333931', '\u0661\u0662\u0663',
                    '/'.join(['1'] * 51)):
            for field in ('ean', 'target_ean'):
                with self.subTest(raw=raw, field=field):
                    data = fixture.fixture(changed=False)
                    data[1][CODE]['detail'][field] = raw
                    with self.assertRaises(scope.Unverified):
                        fixture.inspect(data)

    def test_saved_raw_receipt_and_correction_before_keep_identity_references(self):
        data = fixture.fixture(changed=False)
        raw = 'LEGACY123R'
        data[0]['receipt']['cards'] = {CODE: {'state': 'blocked', 'evidence': {'ean': raw}}}
        self.assertEqual(fixture.inspect(data, aliases={raw.casefold()})['classification'], 'reference')
        data[0]['receipt'] = {}
        data[2]['selection'] = {'barcode_corrections': {CODE: {'before': raw, 'ean': '4006381333931'}}}
        self.assertEqual(fixture.inspect(data, aliases={raw.casefold()})['classification'], 'reference')

    def test_actual_native_dto_and_navigation_do_not_hide_owner_or_parent(self):
        data = fixture.evidence(fixture.fixture(changed=False), vid=101)
        leaf = data[0]['receipt']['cards'][CODE]['evidence']['xtrek']
        identity = RemoteIdentity(2, CODE, True, '101', 'SYN-PRIOR-PARENT', external_product_id='100')
        leaf.update(cards.dto(identity, 70), **source._navigation(
            {'url': 'https://shop.example/SYN-OLD', 'admin_url': 'https://admin.example/100'}, {}))
        self.assertEqual(fixture.inspect(data)['classification'], 'context_only')
        self.assertEqual(fixture.inspect(data, mapping_ids={70})['classification'], 'reference')
        self.assertEqual(fixture.inspect(data, native={('xtrek', 100, 101)})['classification'], 'reference')
        self.assertEqual(fixture.inspect(data, native_parents={('xtrek', 100)})['classification'], 'reference')
        for key, value in [('shop_url', None), ('shop_admin_url', None)]:
            leaf[key] = value
        self.assertNotEqual(fixture.inspect(data)['classification'], 'unknown')

    def test_navigation_accepts_only_two_known_nullable_writer_fields(self):
        for key, value in [('shop_url', {'product_id': 2}), ('shop_admin_url', 2), ('shop_url', False),
                           ('shop_url', 'javascript:bad'), ('future_native_id', 2)]:
            with self.subTest(key=key, value=value):
                data = fixture.evidence(fixture.fixture(changed=False))
                data[0]['receipt']['cards'][CODE]['evidence']['xtrek'][key] = value
                with self.assertRaises(scope.Unverified):
                    fixture.inspect(data)
        data = fixture.evidence(fixture.fixture(changed=False))
        data[3][70]['product_id'] = 2
        with self.assertRaisesRegex(scope.Unverified, 'atomer_saved_mapping_lineage_unverified'):
            fixture.inspect(data)

    def test_actual_kind_conflicts_keep_source_and_native_namespaces_separate(self):
        for target_variant in (False, True):
            with self.subTest(target_variant=target_variant):
                data = kind_conflict(target_variant=target_variant)
                before = deepcopy(data)
                detail = data[1][CODE]['detail']
                self.assertEqual(detail['state'], 'review')
                self.assertIn('kind_differs', detail['reasons'])
                result = fixture.inspect(data)
                self.assertEqual(result['classification'], 'context_only')
                self.assertIn('reported_atomer_source_native_kind_conflict',
                              {r['reason'] for r in result['context_paths']})
                key = ('xtrek', 9001, 9002 if target_variant else None)
                self.assertEqual(fixture.inspect(data, native={key})['classification'], 'reference')
                self.assertEqual(fixture.inspect(data, native_parents={('xtrek', 9001)})['classification'], 'reference')
                self.assertEqual(data, before)

    def test_mismatch_requires_exact_reported_conflict_and_independent_leaf_shape(self):
        for change in ({'state': 'linked'}, {'reasons': []}, {'reasons': ['different_shop_link']},
                       {'reasons': ['kind_differs', {}]}, {'parent': 'OTHER'}, {'target_code': None},
                       {'target_parent': 'NOT-A-STANDALONE'}, {'variant_id': True}, {'product_id': False}):
            with self.subTest(change=change):
                data = kind_conflict()
                data[1][CODE]['detail'].update(change)
                with self.assertRaises(scope.Unverified):
                    fixture.inspect(data)


if __name__ == '__main__':
    unittest.main()
