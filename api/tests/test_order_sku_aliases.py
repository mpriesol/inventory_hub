"""Verified old order codes never relax ordinary cross-system identity rules."""
from collections import defaultdict
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from inventory_hub.db_models import IdentifierType, Product, ProductIdentifier, Shop
from inventory_hub.db_models_ext import ShopProduct
from inventory_hub.product_editor_models import ProductEditorAudit
from inventory_hub.services import order_sku_aliases as service, order_stock_source
from inventory_hub.services.product_identity import IdentityIndex, RemoteIdentity


FINGERPRINT = "f" * 64


def audit(old="OLD-001", new="TP-001", *, variant=False, identifier=71, revision=1):
    parent = "PARENT" if variant else new
    return ProductEditorAudit(id=identifier, product_id=1, revision=revision, before_data={"sku": old},
        after_data={"sku": new, "operation": "verified_code_reconciliation", "native_identities": [{
            "shop": "biketrek", "shop_id": 1, "mapping_id": 11, "native_id": "990" if variant else "100",
            "fingerprint": FINGERPRINT,
            "target": {"code": new, "parent_code": parent, "variant_code": new if variant else None},
            "verified_identity": {"code": new, "parent_code": parent, "variant_code": new if variant else None,
                                  "product_id": 100, "variant_id": 990 if variant else None}}]})


def fixture(*, variant=False, audits=None, identifiers=None, mappings=None):
    products = [Product(id=1, sku="TP-001", name="Canonical"), Product(id=2, sku="OTHER", name="Other")]
    mapping = ShopProduct(
        id=11,
        shop_id=1,
        product_id=1,
        external_code='TP-001',
        parent_code='PARENT' if variant else None,
        is_variant=variant,
        is_listed=True,
        external_product_id=None if variant else '100',
        external_variant_id='990' if variant else None)
    values = [ProductIdentifier(id=1, product_id=1, value="OLD-001", identifier_type=IdentifierType.internal_sku),
              ProductIdentifier(id=2, product_id=1, value="4006381333931", identifier_type=IdentifierType.ean),
              ProductIdentifier(id=3, product_id=2, value="5901234123457", identifier_type=IdentifierType.ean)]
    index = IdentityIndex(1, products, [mapping] if mappings is None else mappings,
                          values if identifiers is None else identifiers)
    by_product = defaultdict(list)
    for row in [audit(variant=variant)] if audits is None else audits:
        by_product[row.product_id].append(row)
    return service.AliasIndex(index, "biketrek", FINGERPRINT, by_product)


def line(**changes):
    return {"code": "OLD-001", "ean": "4006381333931", "has_native_identity": True,
            "native_product_id": 100, "native_option_set_id": None, **changes}


class OrderSkuAliasTests(TestCase):
    def test_exact_alias_requires_audited_shop_scope_and_native_identity(self):
        aliases = fixture()
        self.assertEqual(aliases.index.resolve(RemoteIdentity(1, "OLD-001")).reasons, ["local_mapping_conflict"])
        resolved, proof = aliases.resolve(line())
        self.assertEqual((resolved.status, resolved.product_id, resolved.matched_by), ("mapped", 1, "verified_sku_alias"))
        self.assertEqual((proof["old_code"], proof["current_code"], proof["audit_ids"]), ("OLD-001", "TP-001", [71]))
        self.assertEqual(aliases.index.products[1].sku, "TP-001")

    def test_alias_notes_or_ean_alone_do_not_authorize_a_mapping_crossing(self):
        for changes in ({}, {"ean": ""}):
            result, proof = fixture(audits=[]).resolve(line(**changes))
            self.assertEqual(result.reasons, ["order_sku_alias_unverified"])
            self.assertIsNone(proof)

    def test_barcode_owner_disagreement_and_case_collisions_keep_original_conflicts(self):
        for incoming in (line(ean="5901234123457"), line(ean="1234567890128"), line(code="old-001")):
            result, proof = fixture().resolve(incoming)
            self.assertEqual(result.status, "conflict")
            self.assertIsNone(proof)

    def test_missing_or_repurposed_alias_is_not_recovered_by_name_or_ean(self):
        aliases = fixture()
        for incoming in (line(code="UNKNOWN"), line(code="OTHER")):
            result, proof = aliases.resolve(incoming)
            self.assertEqual(result.status, "conflict")
            self.assertIsNone(proof)

    def test_conflicting_code_in_same_shop_cannot_be_overridden_by_alias_audit(self):
        aliases = fixture()
        aliases.index.add_mapping(ShopProduct(id=12, shop_id=1, product_id=2, external_code='OLD-001', is_variant=False))
        result, proof = aliases.resolve(line())
        self.assertEqual(result.status, "conflict")
        self.assertIsNone(proof)

    def test_native_kind_parent_and_missing_native_facts_block(self):
        for changes in ({"native_product_id": 101}, {"native_product_id": None},
                        {"has_native_identity": False}, {"native_option_set_id": 990}):
            result, proof = fixture().resolve(line(**changes))
            self.assertEqual(result.reasons, ["order_sku_alias_native_conflict"])
            self.assertIsNone(proof)

    def test_variant_uses_audited_leaf_and_parent_without_equating_option_set_id(self):
        aliases = fixture(variant=True)
        result, proof = aliases.resolve(line(native_option_set_id=222))
        self.assertEqual((result.status, proof["native_id"], proof["parent_native_id"]), ("mapped", "990", "100"))
        for changes in ({"native_product_id": 101, "native_option_set_id": 222}, {"native_option_set_id": None}):
            self.assertEqual(aliases.resolve(line(**changes))[0].reasons, ["order_sku_alias_native_conflict"])

    def test_incomplete_or_wrong_audit_never_supplies_a_proof(self):
        for key, value in (("shop", "xtrek"), ("shop_id", 2), ("mapping_id", 12),
                           ("native_id", "999"), ("fingerprint", "x" * 64)):
            row = audit()
            row.after_data["native_identities"][0][key] = value
            with self.subTest(key=key):
                self.assertEqual(fixture(audits=[row]).resolve(line())[0].reasons, ["order_sku_alias_unverified"])
        for key, value in (("product_id", 101), ("variant_id", 990), ("code", "WRONG"), ("parent_code", "WRONG")):
            row = audit()
            row.after_data["native_identities"][0]["verified_identity"][key] = value
            with self.subTest(remote=key):
                self.assertEqual(fixture(audits=[row]).resolve(line())[0].reasons, ["order_sku_alias_unverified"])
        row = audit()
        row.after_data["native_identities"] *= 2
        self.assertIsNone(fixture(audits=[row]).resolve(line())[1])

    def test_changed_connection_mapping_native_id_activity_or_pos_group_blocks(self):
        for key, value in (("external_product_id", "999"), ("is_listed", False), ("external_code", "OTHER"), ("parent_code", "xTrek")):
            aliases = fixture()
            setattr(aliases.index.by_product[1][0], key, value)
            with self.subTest(key=key):
                self.assertIsNone(aliases.resolve(line())[1])
        aliases = fixture()
        aliases.target_fingerprint = None
        self.assertIsNone(aliases.resolve(line())[1])
        aliases = fixture()
        aliases.index.products[1].is_active = False
        self.assertEqual(aliases.resolve(line())[0].reasons, ["inactive_product"])

    def test_variant_parent_code_change_does_not_inherit_an_old_proof(self):
        aliases = fixture(variant=True)
        aliases.index.by_product[1][0].parent_code = "DIFFERENT-PARENT"
        self.assertIsNone(aliases.resolve(line(native_option_set_id=222))[1])

    def test_multihop_aliases_require_an_unbroken_ordered_unique_native_chain(self):
        rows = [audit(new="MID-001"), audit(old="MID-001", identifier=72, revision=2)]
        result, proof = fixture(audits=rows).resolve(line())
        self.assertEqual((result.status, proof["audit_ids"]), ("mapped", [71, 72]))
        for broken in ([rows[0]], [rows[1]], [*rows, audit(new="BRANCH", identifier=73, revision=3)],
                       [rows[0], audit(old="MID-001", identifier=72, revision=1)]):
            self.assertIsNone(fixture(audits=broken).resolve(line())[1])


class OrderSkuAliasReaderTests(IsolatedAsyncioTestCase):
    async def test_current_codes_do_not_query_audits_or_connection(self):
        aliases = fixture()
        with patch.object(service, "fingerprint") as fingerprint:
            loaded = await service.load(None, Shop(id=1, code="biketrek"), aliases.index, [line(code="TP-001")])
        fingerprint.assert_not_called()
        self.assertEqual(loaded.resolve(line(code="TP-001"))[0].matched_by, "shop_mapping")

    async def test_saved_or_forged_proof_is_removed_and_freshly_resolved(self):
        aliases = fixture()
        db = SimpleNamespace(scalars=AsyncMock(return_value=aliases.audits[1]))
        normalized = order_stock_source._line({"uuid": "00000000-0000-0000-0000-000000000001",
            "quantity": "1", "type": "product", "product_id": 100, "unit": "ks", "code": "OLD-001"})
        normalized.update(sku_alias_proof={"invented": True}, classification="mapped", product_id=999)
        with patch.object(order_stock_source, "load_identity_index", AsyncMock(return_value=aliases.index)), \
             patch.object(service, "fingerprint", return_value=FINGERPRINT):
            result = await order_stock_source.resolve_lines(db, Shop(id=1, code="biketrek"), {"lines": [normalized]})
        self.assertEqual((result[0]["product_id"], result[0]["matched_by"]), (1, "verified_sku_alias"))
        self.assertNotIn("invented", result[0]["sku_alias_proof"])
        db.scalars.return_value = []
        with patch.object(order_stock_source, "load_identity_index", AsyncMock(return_value=aliases.index)), \
             patch.object(service, "fingerprint", return_value=FINGERPRINT):
            result = await order_stock_source.resolve_lines(db, Shop(id=1, code="biketrek"), {"lines": result})
        self.assertEqual(result[0]["classification"], "conflict")
        self.assertNotIn("sku_alias_proof", result[0])
