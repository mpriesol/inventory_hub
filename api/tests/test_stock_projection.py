"""Own-stock projection contracts without network or database mutations."""
from contextlib import nullcontext
from decimal import Decimal
from types import SimpleNamespace as Row
import unittest
from unittest.mock import patch

from sqlalchemy.sql import Select

from inventory_hub.services import stock_projection as service


def product(identifier=1, sku="SKU-A", **changes):
    return Row(**{"id": identifier, "sku": sku, "is_active": True, **changes})


def mapping(identifier=1, product_id=1, code="SKU-A", parent="NORMAL-PARENT", **changes):
    return Row(**{"id": identifier, "product_id": product_id, "is_variant": True,
                  "variant_code": code, "external_code": parent, "parent_code": parent,
                  "is_listed": True, **changes})


def balance(product_id=1, on_hand="7", reserved="3", evidence=True):
    return Row(product_id=product_id, qty_on_hand=Decimal(on_hand), qty_reserved=Decimal(reserved), has_movement=evidence)


class Result:
    def __init__(self, rows):
        self.rows = rows

    def one_or_none(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


class ReadOnlyDB:
    no_autoflush = nullcontext()

    def __init__(self, products=None, mappings=None, balances=None, *, stop=None):
        self.calls = []
        self.rows = [[Row(id=11, code="biketrek")], [Row(warehouse_id=21)],
                     [Row(id=21, code="CENTRAL", name="Confirmed central warehouse")],
                     products if products is not None else [product()],
                     mappings if mappings is not None else [mapping()],
                     balances if balances is not None else [balance()]]
        if stop is not None:
            self.rows[stop] = []

    async def execute(self, statement):
        if not isinstance(statement, Select):
            raise AssertionError("Projection attempted a non-SELECT statement")
        self.calls.append(statement)
        return Result(self.rows[len(self.calls) - 1])


class StockProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_own_available_subtracts_all_reservations_and_never_calls_shop(self):
        db = ReadOnlyDB()
        with patch("inventory_hub.services.upgates.UpgatesClient.from_shop", side_effect=AssertionError("No remote access")):
            result = await service.preview(db, "biketrek", ["SKU-A"])
        row = result["rows"][0]
        self.assertEqual((row["qty_on_hand"], row["qty_reserved"], row["qty_available"]), ("7", "3", "4"))
        self.assertTrue(row["quantity_known"])
        self.assertEqual(row["target"], {"parent_code": "NORMAL-PARENT", "variant_code": "SKU-A", "code": "SKU-A"})
        self.assertFalse(result["external_write_enabled"])
        self.assertEqual(result["warehouse"]["id"], 21)
        self.assertEqual(len(db.calls), 6)

    async def test_missing_or_unverified_balance_never_fabricates_zero(self):
        for balances, reason in (([], "balance_missing"), ([balance(on_hand="0", reserved="0", evidence=False)], "balance_unverified")):
            with self.subTest(reason=reason):
                row = (await service.preview(ReadOnlyDB(balances=balances), "biketrek", ["SKU-A"]))["rows"][0]
                self.assertFalse(row["quantity_known"])
                self.assertIsNone(row["qty_available"])
                self.assertIsNone(row["qty_on_hand"])
                self.assertEqual(row["errors"], ["stock_projection_" + reason])
        exhausted = (await service.preview(ReadOnlyDB(balances=[balance(on_hand="0", reserved="0")]), "biketrek", ["SKU-A"]))["rows"][0]
        self.assertTrue(exhausted["quantity_known"])
        self.assertEqual(exhausted["qty_available"], "0")

    async def test_pos_umbrella_remains_one_leaf_with_no_parent_expansion(self):
        mappings = [mapping(parent="xTrek")] + [mapping(index + 2, index + 2, f"OTHER-{index}", "xTrek") for index in range(1500)]
        db = ReadOnlyDB(mappings=mappings)
        result = await service.preview(db, "biketrek", ["SKU-A"])
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["target"]["parent_code"], "xTrek")
        self.assertEqual(result["rows"][0]["qty_available"], "4")
        self.assertEqual(len(db.calls), 6)

    async def test_case_conflict_outside_selection_and_alias_are_blocked(self):
        for mappings, reason in (([mapping(), mapping(2, 99, "sku-a")], "mapping_ambiguous"),
                                 ([mapping(code="ALIAS")], "mapping_alias"),
                                 ([mapping(code=" SKU-A ")], "mapping_alias"),
                                 ([mapping(parent="")], "mapping_invalid"),
                                 ([], "mapping_missing")):
            with self.subTest(reason=reason):
                row = (await service.preview(ReadOnlyDB(mappings=mappings), "biketrek", ["SKU-A"]))["rows"][0]
                self.assertFalse(row["quantity_known"])
                self.assertIsNone(row["qty_available"])
                self.assertIn("stock_projection_" + reason, row["errors"])

    async def test_unmapped_canonical_case_duplicate_blocks_otherwise_known_stock(self):
        db = ReadOnlyDB(products=[product(), product(99, "sku-a")])
        row = (await service.preview(db, "biketrek", ["SKU-A"]))["rows"][0]
        self.assertEqual(row["product_id"], 1)
        self.assertEqual(row["target"]["code"], "SKU-A")
        self.assertEqual(row["errors"], ["stock_projection_product_ambiguous"])
        self.assertFalse(row["quantity_known"])
        self.assertIsNone(row["qty_available"])

    async def test_exact_sku_order_and_standalone_target_are_preserved(self):
        db = ReadOnlyDB(products=[product(1, "SKU-A")], mappings=[mapping(is_variant=False, external_code="SKU-A", variant_code=None, parent_code=None)])
        rows = (await service.preview(db, "biketrek", ["sku-a", "SKU-A"]))["rows"]
        self.assertEqual([row["sku"] for row in rows], ["sku-a", "SKU-A"])
        self.assertEqual(rows[0]["errors"], ["stock_projection_product_missing"])
        self.assertIsNone(rows[0]["qty_on_hand"])
        self.assertEqual(rows[1]["target"], {"parent_code": "SKU-A", "variant_code": None, "code": "SKU-A"})

    async def test_inactive_product_or_unlisted_mapping_has_no_publishable_quantity(self):
        for db, reason in ((ReadOnlyDB(products=[product(is_active=False)]), "product_inactive"),
                           (ReadOnlyDB(mappings=[mapping(is_listed=False)]), "mapping_missing")):
            with self.subTest(reason=reason):
                row = (await service.preview(db, "biketrek", ["SKU-A"]))["rows"][0]
                self.assertEqual(row["errors"], ["stock_projection_" + reason])
                self.assertFalse(row["quantity_known"])
                self.assertIsNone(row["qty_available"])

    async def test_invalid_and_fractional_stock_are_blocked_instead_of_clamped(self):
        for on_hand, reserved, reason in (("-1", "0", "quantity_invalid"), ("1", "2", "quantity_invalid"),
                                          ("2", "-1", "quantity_invalid"), ("NaN", "0", "quantity_invalid"),
                                          ("Infinity", "0", "quantity_invalid"), ("2.5", "0", "unit_unsupported"),
                                          ("2", "0.5", "unit_unsupported")):
            with self.subTest(on_hand=on_hand, reserved=reserved):
                row = (await service.preview(ReadOnlyDB(balances=[balance(on_hand=on_hand, reserved=reserved)]), "biketrek", ["SKU-A"]))["rows"][0]
                self.assertEqual(row["errors"], ["stock_projection_" + reason])
                self.assertFalse(row["quantity_known"])
                self.assertIsNone(row["qty_available"])

    async def test_missing_policy_or_active_scope_fails_before_stock_queries(self):
        for stop, code, status in ((0, "shop_not_found", 404), (1, "not_configured", 409), (2, "warehouse_unavailable", 409)):
            with self.subTest(code=code):
                db = ReadOnlyDB(stop=stop)
                with self.assertRaises(service.StockProjectionError) as raised:
                    await service.preview(db, "biketrek", ["SKU-A"])
                self.assertEqual((raised.exception.code, raised.exception.status), ("stock_projection_" + code, status))
                self.assertEqual(len(db.calls), stop + 1)

    async def test_invalid_selection_is_rejected_before_database(self):
        for skus in ([], ["SKU-A"] * 2, [" SKU-A"], ["SKU-A\n"], ["\ud800"], [1], "SKU-A", [str(n) for n in range(101)]):
            with self.subTest(skus=repr(skus)):
                db = ReadOnlyDB()
                with self.assertRaises(service.StockProjectionError) as raised:
                    await service.preview(db, "biketrek", skus)
                self.assertEqual(raised.exception.status, 422)
                self.assertEqual(db.calls, [])
