"""Per-leaf sale stock deltas preserve native decrements and rounding."""
from decimal import Decimal
from types import SimpleNamespace
import unittest

from inventory_hub.services import stock_delta, stock_sync


class SharedStockDeltaTests(unittest.TestCase):
    def test_one_eight_piece_sale_updates_siblings_without_second_native_debit(self):
        # The native8-piece leaf has already subtracted one sale unit. The Hub
        # decreased the shared pool200→192; sibling leaves have no native debt.
        desired = (192, 24, 1)
        before = (200, 25, 2)
        native = (0, -1, 0)
        self.assertEqual([stock_delta.increment(new, debt, old, 0)
                          for new, old, debt in zip(desired, before, native)], ["-8", "0", "-1"])

    def test_hundred_piece_sale_uses_floors_of_pool_levels(self):
        self.assertEqual(stock_delta.increment(12, 0, 25, 0), "-13")
        self.assertEqual(stock_delta.increment(1, -1, 2, 0), "0")

    def test_same_native_leaf_with_changed_sale_factor_requires_new_baseline(self):
        target = {"code": "PACK-8", "parent_code": "PATCHES", "variant_code": "PACK-8"}
        checkpoint = SimpleNamespace(warehouse_id=2, product_id=7, target_fingerprint="verified",
            target=target, sale_key="77", sale_to_stock_factor=Decimal(8))
        state = {"product_id": 7, "target": target, "sale_key": "77", "sale_to_stock_factor": "8"}
        stock_delta.check_binding(checkpoint, state, 2, "verified")
        for change in ({"sale_key": "78"}, {"sale_to_stock_factor": "100"}):
            with self.subTest(change=change), self.assertRaisesRegex(stock_sync.SyncError, "stock_sync_baseline_changed"):
                stock_delta.check_binding(checkpoint, {**state, **change}, 2, "verified")
