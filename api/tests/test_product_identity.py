"""Shared identity resolver: explicit shop aliases, validated identifiers, no SKU guesses."""
import unittest
from types import SimpleNamespace

from inventory_hub.db_models import IdentifierType
from inventory_hub.routers.upgates_sync import _pull_families, _mark_resolved_duplicates
from inventory_hub.services.product_identity import IdentityIndex, RemoteIdentity, verified_barcodes


EAN_A = "5901234123457"
EAN_B = "4006381333931"
EAN_C = "9780201379624"


def product(id, sku, group_id=None):
    return SimpleNamespace(id=id, sku=sku, group_id=group_id)


def identifier(id, value, kind=IdentifierType.ean):
    return SimpleNamespace(product_id=id, value=value, identifier_type=kind)


def mapping(id, product_id, code, *, shop_id=1, variant=False, external_id=None, legacy_parent=False):
    return SimpleNamespace(id=id, product_id=product_id, shop_id=shop_id, is_variant=variant,
                           external_id=external_id, external_code="PARENT" if legacy_parent else code,
                           variant_code=code if variant else None, parent_code="PARENT" if variant else None)


class IdentityResolverTests(unittest.TestCase):
    def test_same_sku_without_trusted_identity_is_a_visible_conflict(self):
        index = IdentityIndex(1, [product(1, "COMMON")])
        result = index.resolve(RemoteIdentity(1, "COMMON", barcodes=("00012345",)))
        self.assertEqual((result.status, result.reasons, result.candidate_product_ids),
                         ("conflict", ["unmapped_sku_collision"], [1]))

    def test_valid_ean_links_different_shop_code_without_changing_local_sku(self):
        index = IdentityIndex(2, [product(1, "CANONICAL")], identifiers=[identifier(1, EAN_A)])
        result = index.resolve(RemoteIdentity(2, "XTREK-ALIAS", barcodes=(EAN_A,)))
        self.assertEqual((result.status, result.product_id, result.matched_by), ("identified", 1, "validated_barcode"))
        self.assertEqual(index.products[1].sku, "CANONICAL")

    def test_unverified_barcode_and_custom_identifier_never_join_products(self):
        for value, kind in (("00012345", IdentifierType.unverified_barcode), (EAN_A, IdentifierType.custom)):
            with self.subTest(kind=kind):
                index = IdentityIndex(1, [product(1, "OTHER")], identifiers=[identifier(1, value, kind)])
                self.assertEqual(index.resolve(RemoteIdentity(1, "REMOTE", barcodes=(value,))).status, "unresolved")

    def test_both_legacy_variant_mapping_conventions_resolve_the_leaf_only(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                index = IdentityIndex(1, [product(1, "LOCAL")], [mapping(7, 1, "V-M", variant=True, legacy_parent=legacy)])
                self.assertEqual(index.resolve(RemoteIdentity(1, "V-M")).product_id, 1)
                self.assertEqual(index.resolve(RemoteIdentity(1, "PARENT")).status, "unresolved")

    def test_same_remote_numeric_id_does_not_cross_simple_variant_namespaces(self):
        index = IdentityIndex(1, [product(1, "S"), product(2, "V")],
                              [mapping(1, 1, "S", external_id="42"), mapping(2, 2, "V", variant=True, external_id="42")])
        self.assertEqual(index.resolve(RemoteIdentity(1, "S", False, "42")).product_id, 1)
        self.assertEqual(index.resolve(RemoteIdentity(1, "V", True, "42")).product_id, 2)
        self.assertEqual(index.resolve(RemoteIdentity(1, external_id="42")).status, "unresolved")

    def test_remote_id_and_code_pointing_at_different_products_block(self):
        index = IdentityIndex(1, [product(1, "S"), product(2, "OTHER")],
                              [mapping(1, 1, "S", external_id="42"), mapping(2, 2, "OTHER", external_id="99")])
        result = index.resolve(RemoteIdentity(1, "S", False, "99"))
        self.assertEqual(result.reasons, ["ambiguous_shop_mapping"])
        self.assertEqual(result.candidate_product_ids, [1, 2])

    def test_mapping_rejects_conflicting_ean_even_if_new_value_has_no_owner(self):
        index = IdentityIndex(1, [product(1, "S")], [mapping(1, 1, "S")], [identifier(1, EAN_A)])
        result = index.resolve(RemoteIdentity(1, "S", barcodes=(EAN_B,)))
        self.assertEqual(result.reasons, ["mapping_identifier_conflict"])

    def test_two_valid_identifiers_with_different_owners_are_ambiguous(self):
        index = IdentityIndex(1, [product(1, "A"), product(2, "B")],
                              identifiers=[identifier(1, EAN_A), identifier(2, EAN_B)])
        result = index.resolve(RemoteIdentity(1, "NEW", barcodes=(EAN_A, EAN_B)))
        self.assertEqual(result.reasons, ["identifier_conflict"])
        self.assertEqual(result.candidate_product_ids, [1, 2])

    def test_existing_same_shop_mapping_cannot_be_reassigned_to_another_code(self):
        index = IdentityIndex(1, [product(1, "CANONICAL")], [mapping(1, 1, "OLD")], [identifier(1, EAN_A)])
        self.assertEqual(index.resolve(RemoteIdentity(1, "NEW", barcodes=(EAN_A,))).reasons, ["local_mapping_conflict"])

    def test_external_id_change_and_code_rename_require_reconciliation(self):
        index = IdentityIndex(1, [product(1, "S")], [mapping(1, 1, "S", external_id="42")])
        for identity in (RemoteIdentity(1, "S", False, "43"), RemoteIdentity(1, "NEW", False, "42")):
            self.assertEqual(index.resolve(identity).reasons, ["mapping_identity_changed"])

    def test_other_shop_mappings_are_not_used_and_identity_scope_is_enforced(self):
        index = IdentityIndex(2, [product(1, "LOCAL")], [mapping(1, 1, "SHOP-ALIAS", shop_id=1)])
        self.assertEqual(index.resolve(RemoteIdentity(2, "SHOP-ALIAS")).status, "unresolved")
        with self.assertRaises(ValueError):
            index.resolve(RemoteIdentity(1, "SHOP-ALIAS"))

    def test_case_only_code_change_is_a_conflict_not_a_new_product(self):
        index = IdentityIndex(1, [product(1, "local")], [mapping(1, 1, "old-code")])
        self.assertEqual(index.resolve(RemoteIdentity(1, "OLD-CODE")).reasons, ["mapping_identity_changed"])
        self.assertEqual(index.resolve(RemoteIdentity(1, "LOCAL")).reasons, ["unmapped_sku_collision"])

    def test_compound_barcodes_keep_exact_valid_values_and_leading_zeros(self):
        self.assertEqual(verified_barcodes((f"{EAN_A}/00012345;{EAN_B}", EAN_A, "012345678905")),
                         (EAN_A, EAN_B, "012345678905"))
        self.assertEqual(verified_barcodes(("0000000000000", "00000000")), ())


class RemotePreflightTests(unittest.TestCase):
    def test_duplicate_remote_leaf_codes_block_every_affected_family(self):
        families = _pull_families(1, [{"code": "PARENT", "variants": [{"code": "DUP"}]},
                                      {"code": "dup"}, {"code": "GOOD"}])
        self.assertIn("remote_code_duplicate", families[0]["reasons"])
        self.assertIn("remote_code_duplicate", families[1]["reasons"])
        self.assertFalse(families[2]["reasons"])

    def test_duplicate_remote_ids_and_barcodes_do_not_depend_on_listing_order(self):
        for field, value, reason in (("product_id", 42, "remote_id_duplicate"),
                                     ("ean", EAN_A, "remote_barcode_duplicate")):
            families = _pull_families(1, [{"code": "A", field: value}, {"code": "B", field: value}])
            self.assertTrue(all(reason in family["reasons"] for family in families))

    def test_missing_variant_code_blocks_entire_family_and_invalid_shape_is_visible(self):
        families = _pull_families(1, [{"code": "PARENT", "variants": [{"code": "V"}, {"ean": EAN_A}]},
                                      {"code": "BAD", "variants": 7}])
        self.assertTrue(all("remote_identity_invalid" in family["reasons"] for family in families))

    def test_parent_without_code_keeps_first_variant_selection_key(self):
        families = _pull_families(1, [{"product_id": 42, "variants": [{"code": "V", "variant_id": 99}]}])
        self.assertEqual(families[0]["code"], "V")
        self.assertFalse(families[0]["reasons"])

    def test_different_remote_barcodes_resolving_same_product_block_both_families(self):
        families = _pull_families(1, [{"code": "A", "ean": EAN_A}, {"code": "B", "ean": EAN_B}])
        index = IdentityIndex(1, [product(1, "LOCAL")], identifiers=[identifier(1, EAN_A), identifier(1, EAN_B)])
        _mark_resolved_duplicates(families, index)
        self.assertTrue(all("local_mapping_conflict" in family["reasons"] for family in families))
