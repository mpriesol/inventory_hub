import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4
from pydantic import ValidationError
from inventory_hub.stock_adjustment_types import StockAdjustmentPreview, StockAdjustmentApply
from inventory_hub.services.stock_adjustments import validate_count
from inventory_hub.services.fifo import FifoError


class StockAdjustmentInputTests(unittest.TestCase):
    def data(self, **changes):
        return {"request_id": str(uuid4()), "sku": "SKU-1", "warehouse_code": "main", "counted_quantity": "3",
            "counted_at": datetime.now(timezone.utc), "source_reference": "Count-1", "operator_name": "Operator",
            "reason": "Physical recount", **changes}

    def test_quantities_are_explicit_whole_counts_not_json_numbers(self):
        for value in [3, True, "-1", "1.5", "NaN", "1000000000", ""]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                StockAdjustmentPreview(**self.data(counted_quantity=value))
        self.assertEqual(StockAdjustmentPreview(**self.data(counted_quantity="0")).counted_quantity, "0")

    def test_unknown_cost_never_becomes_zero(self):
        self.assertIsNone(StockAdjustmentPreview(**self.data()).unit_cost)
        for values in [{"unit_cost": "0"}, {"cost_status": "known"}, {"unit_cost": "-1", "cost_status": "known"}]:
            with self.assertRaises(ValidationError):
                StockAdjustmentPreview(**self.data(**values))
        self.assertEqual(StockAdjustmentPreview(**self.data(cost_status="known", unit_cost="0")).unit_cost, "0")

    def test_confirmations_cannot_be_strings_or_integers(self):
        for value in [1, "true", False]:
            with self.assertRaises(ValidationError):
                StockAdjustmentApply(preview_hash="a" * 64, confirmed=value, quantities_verified=True, costs_documented=True)

    def test_preserves_reservations_and_quarantine(self):
        stock = SimpleNamespace(qty_on_hand=Decimal(10), qty_reserved=Decimal(3), qty_quarantined=Decimal(2))
        with self.assertRaisesRegex(FifoError, "below_committed"):
            validate_count(stock, Decimal(4))
        self.assertEqual(validate_count(stock, Decimal(5)), Decimal(-5))
        with self.assertRaisesRegex(FifoError, "no_change"):
            validate_count(stock, Decimal(10))
