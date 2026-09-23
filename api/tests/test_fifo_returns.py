"""Return selection and net expense rules do not fabricate unknown acquisition cost."""
import unittest
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from pydantic import ValidationError

from inventory_hub.fifo_return_types import FifoCostRevisionInput, FifoReleaseInput, FifoReturnInput
from inventory_hub.services import fifo_returns as service

D = Decimal


def allocation(identifier, quantity, *, returned="0", before=None, price="2", status="known"):
    return SimpleNamespace(id=identifier, sequence=identifier, quantity=D(quantity),
        returned_quantity=D(returned), quantity_before=D(before or quantity),
        unit_cost_current=None if price is None else D(price), cost_status_current=status)


class FifoReturnRuleTests(unittest.TestCase):
    def test_latest_consumed_allocations_return_first_and_repeated_returns_cap(self):
        first = allocation(1, "3")
        second = allocation(2, "2", returned="1")
        self.assertEqual([(row.id, qty) for row, qty in service.plan_return([first, second], D("3"))],
                         [(2, D("1")), (1, D("2"))])
        with self.assertRaisesRegex(service.FifoReturnError, "quantity_exceeds_issue"):
            service.plan_return([first, second], D("5"))

    def test_whole_return_cannot_silently_split_fractional_allocation_cost(self):
        with self.assertRaisesRegex(service.FifoReturnError, "fractional_allocation_unsupported"):
            service.plan_return([allocation(1, "0.5"), allocation(2, "0.5")], D("1"))

    def test_unknown_partial_return_then_reissue_is_counted_only_once(self):
        sold = allocation(1, "5", returned="2", price=None, status="unknown")
        reissued = allocation(2, "1", price=None, status="unknown")
        report = service.consumption_summary([sold, reissued])
        self.assertEqual(report["quantity"], "4")
        self.assertEqual(report["unknown_qty"], "4")
        self.assertIsNone(report["total_cost"])
        self.assertFalse(report["value_complete"])
        for row in (sold, reissued):
            row.unit_cost_current, row.cost_status_current = D("7.5"), "known"
        report = service.consumption_summary([sold, reissued])
        self.assertEqual(D(report["total_cost"]), D("30"))
        self.assertTrue(report["value_complete"])

    def test_fully_returned_unknown_does_not_poison_known_net_expense(self):
        report = service.consumption_summary([allocation(1, "3", returned="3", price=None, status="unknown"),
                                              allocation(2, "2", price="7")])
        self.assertEqual(D(report["total_cost"]), D("14"))
        self.assertEqual(report["unknown_qty"], "0")
        self.assertTrue(report["value_complete"])

    def test_provisional_is_estimated_not_complete(self):
        report = service.consumption_summary([allocation(1, "3", price="1.5", status="provisional"),
                                              allocation(2, "1", price="2")])
        self.assertEqual(D(report["total_cost"]), D("6.5"))
        self.assertEqual(D(report["known_cost"]), D("2"))
        self.assertEqual(D(report["provisional_cost"]), D("4.5"))
        self.assertFalse(report["value_complete"])

    def test_latest_units_reverse_residual_cost(self):
        row = allocation(1, "0.002", before="0.003", returned="0.001", price="0.05")
        self.assertEqual(service.consumed_cost(row.quantity_before, row.quantity, row.unit_cost_current), D("0.0001"))
        self.assertEqual(D(service.consumption_summary([row])["total_cost"]), D("0.0001"))

    def test_physical_assertions_and_decimal_pieces_are_strict(self):
        payload = dict(request_id=str(uuid4()), issue_movement_id=1, quantity="2", case_reference="RMA-1",
                       reason="Physical goods returned", condition="good", confirmed=True, physical_received=True)
        self.assertEqual(FifoReturnInput(**payload).quantity, D("2"))
        for field, value in [("physical_received", 1), ("physical_received", "true"),
                             ("confirmed", False), ("quantity", 2), ("quantity", "0.5"),
                             ("quantity", "NaN"), ("quantity", "-1"), ("case_reference", " "),
                             ("reason", "line\nbreak"), ("issue_movement_id", True)]:
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                FifoReturnInput(**{**payload, field: value})
        with self.assertRaises(ValidationError):
            FifoReleaseInput(request_id=str(uuid4()), source_layer_id=1, target_warehouse_id=2,
                quantity="1", reason="Checked", confirmed=True, condition_verified=False)

    def test_unknown_is_null_and_documented_cost_revision_is_strict(self):
        payload = dict(request_id=str(uuid4()), root_layer_id=1, expected_revision=0, new_unit_cost=None,
                       cost_status="unknown", reason="Invoice missing", document_reference="case-1", confirmed=True)
        self.assertIsNone(FifoCostRevisionInput(**payload).new_unit_cost)
        for update in ({"new_unit_cost": "0"}, {"cost_status": "known"}, {"cost_status": "provisional"},
                       {"new_unit_cost": 2, "cost_status": "known"}, {"expected_revision": True},
                       {"document_reference": ""}, {"new_unit_cost": "1.12345", "cost_status": "known"}):
            with self.subTest(update=update), self.assertRaises(ValidationError):
                FifoCostRevisionInput(**{**payload, **update})
        self.assertEqual(FifoCostRevisionInput(**{**payload, "new_unit_cost": "0", "cost_status": "known"}).new_unit_cost, 0)
