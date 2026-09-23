"""Decimal FIFO valuation rules and operator input without database or shop I/O."""
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4
from pydantic import ValidationError
from inventory_hub.fifo_types import FifoCutoverApply, FifoCutoverLayer, FifoReceiptPreview
from inventory_hub.services import fifo

D = Decimal


def layer(quantity, price, status="known", stock_status="available"):
    return SimpleNamespace(quantity_remaining=D(quantity), unit_cost=None if price is None else D(price),
                           cost_status=status, stock_status=stock_status)


class FifoRuleTests(unittest.TestCase):
    def test_remaining_average_is_recalculated_from_remaining_layers(self):
        before = fifo.summarize([layer("1", "80"), layer("1", "90")])
        after = fifo.summarize([layer("0", "80"), layer("1", "90")])
        self.assertEqual((before["avg_cost"], after["avg_cost"], after["total_value"]), ("85", "90", "90"))

    def test_unknown_quantity_keeps_known_subtotal_but_never_full_zero_valuation(self):
        value = fifo.summarize([layer("1", "80"), layer("1", None, "unknown")])
        self.assertEqual((value["known_value"], value["unknown_qty"]), ("80", "1"))
        self.assertIsNone(value["avg_cost"])
        self.assertIsNone(value["total_value"])
        self.assertFalse(value["value_complete"])
        self.assertIsNone(fifo.allocation_cost(D("2"), D("1"), None))

    def test_provisional_value_is_priced_but_incomplete_and_quarantine_is_separate(self):
        value = fifo.summarize([layer("1", "80", "known"), layer("1", "90", "provisional", "quarantine")])
        self.assertEqual((value["total_value"], value["known_value"], value["provisional_value"]), ("170", "80", "90"))
        self.assertEqual((value["provisional_qty"], value["quarantined_qty"]), ("1", "1"))
        self.assertFalse(value["value_complete"])

    def test_partial_layer_costs_conserve_rounded_original_value(self):
        price = D("0.1234")
        first = fifo.allocation_cost(D("1"), D("0.333"), price)
        second = fifo.allocation_cost(D("0.667"), D("0.333"), price)
        last = fifo.allocation_cost(D("0.334"), D("0.334"), price)
        self.assertEqual(first + second + last, D("0.1234"))
        self.assertEqual(last, fifo.money(D("0.334") * price))

    def test_exhausted_unknown_layer_does_not_hide_known_remaining_inventory(self):
        value = fifo.summarize([layer("0", None, "unknown"), layer("1", "90")])
        self.assertTrue(value["value_complete"])
        self.assertEqual(value["avg_cost"], "90")

    def test_cost_classification_and_quantity_scale_require_documented_exact_input(self):
        base = dict(quantity="1", unit_cost="80", cost_status="known", physical_received_at=datetime.now(timezone.utc),
                    source_reference="Supplier invoice")
        for update in ({"quantity": "1.0001"}, {"quantity": 1}, {"unit_cost": "0.00001"},
                       {"unit_cost": None}, {"cost_status": "unknown"}, {"quantity": "NaN"}, {"source_reference": " "}, {"source_reference": "A\nB"}):
            with self.subTest(update=update), self.assertRaises(ValidationError):
                FifoCutoverLayer(**{**base, **update})
        unknown = FifoCutoverLayer(**{**base, "unit_cost": None, "cost_status": "unknown"})
        self.assertIsNone(unknown.unit_cost)
        with self.assertRaises(ValidationError):
            FifoCutoverApply(preview_hash="a" * 64, confirmed=1, quantities_verified=True, costs_documented=True)
        with self.assertRaises(ValidationError):
            FifoReceiptPreview(**base, request_id=uuid4(), sku=" SKU", warehouse_code="central", operator_name="Operator")

    def test_aggregate_value_overflow_is_rejected_before_database_assignment(self):
        with self.assertRaises(fifo.FifoError) as raised:
            fifo.summarize([layer("999999999", "99999999.9999")])
        self.assertEqual(raised.exception.code, "fifo_value_out_of_range")

    def test_tiny_quantity_rounding_cannot_overflow_numeric_average(self):
        with self.assertRaises(fifo.FifoError) as raised:
            fifo.summarize([layer("0.001", "99999999.9999")])
        self.assertEqual(raised.exception.code, "fifo_value_out_of_range")
