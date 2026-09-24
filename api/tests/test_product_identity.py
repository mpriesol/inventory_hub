"""Shared identity resolver: explicit mappings, exact shared SKUs, validated barcodes."""
import unittest
from types import SimpleNamespace

from inventory_hub.db_models import IdentifierType, Product, ProductIdentifier
from inventory_hub.db_models_ext import ShopProduct
from inventory_hub.routers.upgates_sync import _pull_families, _mark_resolved_duplicates
from inventory_hub.services.product_identity import IdentityIndex, RemoteIdentity, load_identity_index, verified_barcodes


EAN_A = "5901234123457"
EAN_B = "4006381333931"
EAN_C = "9780201379624"


def product(id, sku, group_id=None):
    return SimpleNamespace(id=id, sku=sku, group_id=group_id)


def identifier(id, value, kind=IdentifierType.ean, supplier_id=None):
    return SimpleNamespace(product_id=id, value=value, identifier_type=kind, supplier_id=supplier_id)


def mapping(id, product_id, code, *, shop_id=1, variant=False, external_id=None, legacy_parent=False):
    return SimpleNamespace(id=id, product_id=product_id, shop_id=shop_id, is_variant=variant,
                           external_id=external_id, external_code="PARENT" if legacy_parent else code,
                           variant_code=code if variant else None, parent_code="PARENT" if variant else None)


class IdentityResolverTests(unittest.TestCase):
    def test_exact_shared_sku_identifies_product_without_requiring_ean(self):
        for barcodes in ((), ("00012345",), ("0000000000000",)):
            with self.subTest(barcodes=barcodes):
                index = IdentityIndex(2, [product(1, "COMMON")], identifiers=[identifier(1, EAN_A)])
                result = index.resolve(RemoteIdentity(2, "COMMON", barcodes=barcodes))
                self.assertEqual((result.status, result.product_id, result.matched_by), ("identified", 1, "shared_sku"))
                self.assertEqual(result.reasons, [])

    def test_exact_shared_sku_agrees_with_ean_or_allows_first_unowned_ean(self):
        for identifiers in ([], [identifier(1, EAN_A)]):
            with self.subTest(stored=bool(identifiers)):
                index = IdentityIndex(2, [product(1, "COMMON")], identifiers=identifiers)
                result = index.resolve(RemoteIdentity(2, "COMMON", barcodes=(EAN_A,)))
                self.assertEqual((result.status, result.product_id, result.matched_by), ("identified", 1, "shared_sku"))

    def test_shared_sku_and_ean_with_different_owners_conflict(self):
        index = IdentityIndex(2, [product(1, "COMMON"), product(2, "OTHER")],
                              identifiers=[identifier(1, EAN_A), identifier(2, EAN_B)])
        result = index.resolve(RemoteIdentity(2, "COMMON", barcodes=(EAN_B,)))
        self.assertEqual((result.status, result.reasons, result.candidate_product_ids),
                         ("conflict", ["identifier_conflict"], [1, 2]))

    def test_shared_sku_conflicts_with_disjoint_known_ean_even_if_incoming_unowned(self):
        index = IdentityIndex(2, [product(1, "COMMON")], identifiers=[identifier(1, EAN_A)])
        result = index.resolve(RemoteIdentity(2, "COMMON", barcodes=(EAN_B,)))
        self.assertEqual((result.status, result.reasons, result.candidate_product_ids),
                         ("conflict", ["identifier_conflict"], [1]))

    def test_duplicate_exact_or_casefold_skus_never_choose_an_arbitrary_owner(self):
        for second in ("COMMON", "common"):
            with self.subTest(second=second):
                index = IdentityIndex(2, [product(1, "COMMON"), product(2, second)], identifiers=[identifier(1, EAN_A)])
                result = index.resolve(RemoteIdentity(2, "COMMON", barcodes=(EAN_A,)))
                self.assertEqual((result.status, result.reasons, result.candidate_product_ids),
                                 ("conflict", ["unmapped_sku_collision"], [1, 2]))

    def test_shared_sku_cannot_replace_a_different_existing_same_shop_alias(self):
        index = IdentityIndex(2, [product(1, "COMMON")], [mapping(7, 1, "OLD-ALIAS", shop_id=2)])
        result = index.resolve(RemoteIdentity(2, "COMMON"))
        self.assertEqual(result.reasons, ["local_mapping_conflict"])

    def test_mapping_and_shared_sku_pointing_at_different_products_conflict(self):
        index = IdentityIndex(2, [product(1, "CANONICAL"), product(2, "COMMON")],
                              [mapping(7, 1, "COMMON", shop_id=2)], [identifier(1, EAN_A)])
        result = index.resolve(RemoteIdentity(2, "COMMON", barcodes=(EAN_A,)))
        self.assertEqual((result.status, result.reasons, result.candidate_product_ids),
                         ("conflict", ["mapping_identifier_conflict"], [1, 2]))

    def test_matching_mapping_remains_authoritative_and_is_not_replaced(self):
        stored_mapping = mapping(7, 1, "COMMON", shop_id=2, variant=True, external_id="15")
        index = IdentityIndex(2, [product(1, "COMMON")], [stored_mapping])
        result = index.resolve(RemoteIdentity(2, "COMMON", True, "15"))
        self.assertEqual((result.status, result.product_id, result.matched_by), ("mapped", 1, "shop_mapping"))
        self.assertEqual((stored_mapping.product_id, stored_mapping.variant_code, stored_mapping.external_id), (1, "COMMON", "15"))

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


    def test_local_supplier_alias_cannot_override_the_shared_sku_or_barcode(self):
        index = IdentityIndex(0, [product(1, "SHARED"), product(2, "OTHER")], identifiers=[
            identifier(2, "supplier-code", IdentifierType.supplier_sku, supplier_id=7),
            identifier(1, EAN_A)])
        result = index.resolve(RemoteIdentity(0, "SHARED", barcodes=(EAN_A,),
            supplier_id=7, supplier_sku="supplier-code"))
        self.assertEqual(result.status, "conflict")
        self.assertEqual(result.reasons, ["supplier_identifier_conflict"])
        self.assertEqual(result.candidate_product_ids, [1, 2])

    def test_supplier_codes_are_scoped_and_unverified_barcodes_do_not_join(self):
        index = IdentityIndex(0, [product(1, "A"), product(2, "B")], identifiers=[
            identifier(1, "CODE", IdentifierType.supplier_sku, supplier_id=7),
            identifier(2, "CODE", IdentifierType.supplier_sku, supplier_id=8),
            identifier(1, "12345", IdentifierType.unverified_barcode),
            identifier(2, "12345", IdentifierType.unverified_barcode)])
        for supplier_id, product_id in ((7, 1), (8, 2)):
            result = index.resolve(RemoteIdentity(0, barcodes=("12345",), supplier_id=supplier_id, supplier_sku="CODE"))
            self.assertEqual(result.product_id, product_id)
            self.assertEqual(result.matched_by, "supplier_sku")
        self.assertEqual(index.resolve(RemoteIdentity(0, barcodes=("12345",))).status, "unresolved")

    def test_assigned_product_is_revalidated_against_current_code_and_ean(self):
        index = IdentityIndex(0, [product(1, "SHARED"), product(2, "OTHER")],
            identifiers=[identifier(2, EAN_A)])
        result = index.resolve(RemoteIdentity(0, "SHARED", barcodes=(EAN_A,), expected_product_id=1))
        self.assertEqual(result.status, "conflict")
        self.assertEqual(result.reasons, ["assigned_product_changed"])
        self.assertEqual(index.resolve(RemoteIdentity(0, expected_product_id=99)).reasons, ["assigned_product_missing"])

    def test_unflushed_orm_active_default_does_not_change_identity_classification(self):
        pending_product = Product(id=1, sku="SHARED", name="Canonical")
        self.assertIsNone(pending_product.is_active, "ORM defaults apply on insert, not construction")
        index = IdentityIndex(1, [pending_product])
        result = index.resolve(RemoteIdentity(1, "SHARED"))
        self.assertEqual((result.status, result.product_id, result.matched_by), ("identified", 1, "shared_sku"))
        pending_product.is_active = False
        self.assertEqual(index.resolve(RemoteIdentity(1, "SHARED")).reasons, ["inactive_product"])

    def test_assigned_product_without_new_evidence_and_inactive_product(self):
        active = product(1, "SHARED")
        inactive = product(2, "OLD")
        inactive.is_active = False
        index = IdentityIndex(0, [active, inactive])
        result = index.resolve(RemoteIdentity(0, expected_product_id=1))
        self.assertEqual((result.product_id, result.matched_by), (1, "assigned_product"))
        self.assertEqual(index.resolve(RemoteIdentity(0, "OLD")).reasons, ["inactive_product"])


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


class ReadOnlyIdentityDatabase:
    """Execute the loader's SELECT filters against in-memory relational fixtures."""
    def __init__(self, *, products=(), mappings=(), identifiers=()):
        self.products, self.mappings, self.identifiers = list(products), list(mappings), list(identifiers)
        self.queries = []

    async def execute(self, statement):
        self.queries.append(statement)
        entity = statement.column_descriptions[0]["entity"]
        params = statement.compile().params
        if entity is ShopProduct:
            rows = [row for row in self.mappings if row.shop_id == params["shop_id_1"]]
        elif entity is Product:
            if "lower_1" in params:
                rows = [row for row in self.products if row.sku.lower() in params["lower_1"]]
            else:
                rows = [row for row in self.products if row.id in params["id_1"]]
        elif entity is ProductIdentifier:
            kinds = params["identifier_type_1"]
            if not isinstance(kinds, (tuple, list)):
                kinds = [kinds]
            rows = [row for row in self.identifiers if row.identifier_type in kinds]
            if "value_1" in params:
                rows = [row for row in rows if row.value in params["value_1"]]
            elif "param_1" in params:
                rows = [row for row in rows if (row.supplier_id, row.value) in params["param_1"]]
            else:
                rows = [row for row in rows if row.product_id in params["product_id_1"]]
        else:
            raise AssertionError(f"Unexpected database write or entity: {entity}")
        return SimpleNamespace(scalars=lambda: rows)


class IdentityLoaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_sku_only_candidate_loads_stored_barcodes_before_resolving_unowned_ean(self):
        db = ReadOnlyIdentityDatabase(products=[product(1, "COMMON")], identifiers=[identifier(1, EAN_A)])
        incoming = RemoteIdentity(2, "COMMON", barcodes=(EAN_B,))
        index = await load_identity_index(db, 2, [incoming])
        self.assertEqual(index.product_barcodes[1], {EAN_A})
        self.assertEqual(index.resolve(incoming).reasons, ["identifier_conflict"])
        self.assertTrue(all(str(query).startswith("SELECT ") for query in db.queries))

    async def test_loader_keeps_mapping_sku_and_barcode_candidates_for_conflict_evidence(self):
        db = ReadOnlyIdentityDatabase(products=[product(1, "CANONICAL"), product(2, "COMMON"), product(3, "EAN-OWNER")],
            mappings=[mapping(7, 1, "COMMON", shop_id=2)], identifiers=[identifier(3, EAN_A)])
        incoming = RemoteIdentity(2, "COMMON", barcodes=(EAN_A,))
        index = await load_identity_index(db, 2, [incoming])
        self.assertEqual(index.resolve(incoming).candidate_product_ids, [1, 2, 3])
        self.assertEqual(index.resolve(incoming).status, "conflict")

    async def test_thousand_shared_skus_use_bounded_read_batches_not_per_line_queries(self):
        products = [product(i, f"SHARED-{i:04d}") for i in range(1, 1002)]
        db = ReadOnlyIdentityDatabase(products=products, identifiers=[identifier(1, EAN_A)])
        incoming = [RemoteIdentity(2, row.sku, True) for row in products]
        index = await load_identity_index(db, 2, incoming)
        self.assertTrue(all(index.resolve(row).matched_by == "shared_sku" for row in incoming))
        self.assertLessEqual(len(db.queries), 8)
        for query in db.queries:
            self.assertTrue(str(query).startswith("SELECT "))
            for value in query.compile().params.values():
                if isinstance(value, (list, tuple)):
                    self.assertLessEqual(len(value), 500)
        self.assertEqual(index.product_barcodes[1], {EAN_A})


    async def test_local_loader_batches_supplier_aliases_and_assigned_products_without_shop_mappings(self):
        db = ReadOnlyIdentityDatabase(products=[product(1, "CANONICAL"), product(2, "OTHER")],
            mappings=[mapping(7, 1, "SHOP-ALIAS", shop_id=1)], identifiers=[
                identifier(1, "RAW", IdentifierType.supplier_sku, supplier_id=7),
                identifier(2, "RAW", IdentifierType.supplier_sku, supplier_id=8), identifier(1, EAN_A)])
        incoming = RemoteIdentity(0, "NEW-ALIAS", barcodes=(EAN_A,), supplier_id=7,
            supplier_sku="RAW", expected_product_id=1)
        index = await load_identity_index(db, 0, [incoming], local=True)
        self.assertEqual(index.resolve(incoming).product_id, 1)
        self.assertEqual(index.by_supplier_sku[(7, "RAW")], {1})
        self.assertFalse(index.by_supplier_sku.get((8, "RAW")))
        self.assertTrue(all(query.column_descriptions[0]["entity"] is not ShopProduct for query in db.queries))
        self.assertTrue(all(str(query).startswith("SELECT ") for query in db.queries))
