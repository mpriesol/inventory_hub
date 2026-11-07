"""Synthetic explicit-review boundaries; ordinary matching is unchanged."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from inventory_hub.db_models import IdentifierType
from inventory_hub.product_link_types import ProductLinkMatch
from inventory_hub.services import product_link_recovery as recovery, product_links as links
from inventory_hub.services.product_identity import IdentityIndex


EAN_A, EAN_B = '5901234123457', '4006381333931'


def review(mode='verified_barcode_subset', **changes):
    return {'mapping_id': 7, 'native_product_id': 800, 'native_variant_id': 101,
            'mode': mode, 'identity_evidence': 'Reviewed the same physical fixture with its exact axes.',
            'same_physical_item_confirmed': True, **changes}


class ProductLinkRecoveryContractTests(unittest.TestCase):
    def match(self, **changes):
        return ProductLinkMatch(product_id=1, code='PHYSICAL-A', parent_code='FAMILY', is_variant=True,
                                **changes)

    def test_new_modes_require_typed_native_pins_and_literal_review(self):
        self.assertEqual(self.match(reviewed_deleted_replacement=review()).reviewed_deleted_replacement.mapping_id, 7)
        for field, value in (('mapping_id', True), ('mapping_id', '7'), ('native_product_id', 800.0),
                             ('native_variant_id', None), ('native_product_id', 0),
                             ('same_physical_item_confirmed', 1), ('same_physical_item_confirmed', 'true'),
                             ('identity_evidence', 'unreviewed\nsource'), ('mode', 'override')):
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                self.match(reviewed_deleted_replacement=review(**{field: value}))

    def test_no_barcode_requires_a_structured_source_and_modes_cannot_mix(self):
        with self.assertRaises(ValidationError):
            self.match(reviewed_deleted_replacement=review('exact_sku_without_barcode'))
        item = review('exact_sku_without_barcode', evidence_source={'kind': 'shop_mapping', 'id': 8})
        self.match(reviewed_deleted_replacement=item)
        for source in ({'kind': 'text', 'id': 8}, {'kind': 'supplier_product', 'id': True}):
            with self.subTest(source=source), self.assertRaises(ValidationError):
                self.match(reviewed_deleted_replacement={**item, 'evidence_source': source})
        for flag in ('replace_deleted_variant', 'replace_deleted_product'):
            with self.subTest(flag=flag), self.assertRaises(ValidationError):
                self.match(reviewed_deleted_replacement=item, **{flag: True})

    def test_parent_completion_only_targets_an_explicit_different_variant_parent(self):
        item = {k: v for k, v in review().items() if k != 'mode'}
        item['expected_parent_code'] = 'PHYSICAL-A'
        self.match(complete_unverified_parent=item)
        for changes in ({'expected_parent_code': 'FAMILY'}, {'native_variant_id': None}, {'expected_parent_code': ''}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.match(complete_unverified_parent={**item, **changes})
        with self.assertRaises(ValidationError):
            self.match(complete_unverified_parent=item, reviewed_deleted_replacement=review())

    def test_only_explicit_valid_subset_or_empty_exact_sku_can_replace(self):
        facts = {'sku': 'PHYSICAL-A', 'eans': [EAN_A, EAN_B], 'manual_eans': None}
        remote = {'code': 'PHYSICAL-A', 'eans': [EAN_A], 'invalid_barcodes': []}
        previous = {'external_code': 'PHYSICAL-A'}
        subset = {'request': review(), 'source': None}
        self.assertIsNone(recovery.replacement_identity_error(facts, remote, previous, subset))
        for local, target in ((facts, {**remote, 'eans': []}),
                              ({**facts, 'eans': [EAN_B]}, remote),
                              ({**facts, 'manual_eans': []}, remote),
                              (facts, {**remote, 'invalid_barcodes': ['bad']})):
            with self.subTest(local=local, target=target):
                self.assertIsNotNone(recovery.replacement_identity_error(local, target, previous, subset))
        no_ean = {'request': review('exact_sku_without_barcode'), 'source': {'source_hash': 'a' * 64}}
        facts, remote = {**facts, 'eans': []}, {**remote, 'eans': []}
        self.assertIsNone(recovery.replacement_identity_error(facts, remote, previous, no_ean))
        for local, target, old, proof in (
                ({**facts, 'eans': [EAN_A]}, remote, previous, no_ean),
                (facts, {**remote, 'eans': [EAN_A]}, previous, no_ean),
                (facts, remote, {'external_code': 'OTHER'}, no_ean),
                ({**facts, 'sku': 'OTHER'}, remote, previous, no_ean),
                (facts, {**remote, 'invalid_barcodes': ['bad']}, previous, no_ean),
                (facts, remote, previous, {**no_ean, 'source': None})):
            with self.subTest(local=local, target=target, old=old, proof=proof):
                self.assertIsNotNone(recovery.replacement_identity_error(local, target, old, proof))

    def test_parent_family_selection_is_indivisible(self):
        row = {'parent_completion': {'family_product_ids': [1, 2]}}
        recovery.require_complete_selection([row], [1, 2, 3])
        with self.assertRaisesRegex(links.LinkError, 'product_links_parent_family_incomplete'):
            recovery.require_complete_selection([row], [1])

    def test_old_cache_with_a_known_different_native_parent_or_leaf_cannot_be_archived(self):
        from datetime import datetime, timezone
        remote = {'code': 'PHYSICAL-A', 'external_id': '101'}
        good = {'product_id': 800, 'code': None, 'variants': [{'code': 'PHYSICAL-A', 'variant_id': 101}]}
        for change in ({'product_id': 999}, {'code': 'ACTUAL-OLD-PARENT'},
                       {'variants': [{'code': 'PHYSICAL-A', 'variant_id': 999}]},
                       {'_hub_link_metadata': {'variant:101': {'parent_id': '999'}}}):
            row = SimpleNamespace(id=4, external_code='PHYSICAL-A', data={**good, **change},
                                  pulled_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
            with self.subTest(change=change), self.assertRaisesRegex(links.LinkError, 'product_links_recovery_content_conflict'):
                recovery._check_parent_content(row, '800', 'PHYSICAL-A', [remote], legacy=True)


class ProductLinkRecoveryOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_review_never_overrides_foreign_sku_barcode_or_axes(self):
        facts = {'product_id': 1, 'sku': 'PHYSICAL-A', 'name': 'Fixture', 'is_active': True,
                 'eans': [EAN_A], 'manual_eans': None, 'attributes': [{'name': 'size', 'value': 'M'}]}
        target = {'code': 'REMOTE-A', 'parent_code': 'FAMILY', 'is_variant': True,
                  'parent_id': '800', 'external_id': '101', 'name': 'Fixture', 'eans': [EAN_A],
                  'attributes': [{'name': 'size', 'value': 'M'}]}
        product = SimpleNamespace(id=1, sku='PHYSICAL-A', is_active=True)
        identifier = SimpleNamespace(product_id=1, value=EAN_A, identifier_type=IdentifierType.ean, supplier_id=None)
        for foreign_sku, foreign_ean, size in ((True, False, 'M'), (False, True, 'M'), (False, False, 'L')):
            other = SimpleNamespace(id=2, sku='REMOTE-A' if foreign_sku else 'OTHER', is_active=True)
            identifiers = [identifier]
            if foreign_ean:
                identifiers.append(SimpleNamespace(product_id=2, value=EAN_A,
                    identifier_type=IdentifierType.ean, supplier_id=None))
            index = IdentityIndex(2, [product, other], [], identifiers)
            remote = {**deepcopy(target), 'attributes': [{'name': 'size', 'value': size}]}
            with patch.object(links, 'load_identity_index', AsyncMock(return_value=index)):
                rows = await links._rows(SimpleNamespace(), SimpleNamespace(id=2), {1: facts},
                    {1: (remote, 'manual', 1)}, {1: {'id': 7, 'external_code': 'OLD', 'is_variant': True}},
                    {1: {'request': review(), 'source': None}})
            with self.subTest(foreign_sku=foreign_sku, foreign_ean=foreign_ean, size=size):
                self.assertEqual(rows[0]['status'], 'conflict')
                self.assertNotIn('product_links_replacement_ean_mismatch', rows[0]['errors'])


if __name__ == '__main__':
    unittest.main()
