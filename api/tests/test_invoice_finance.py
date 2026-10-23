"""Exact amounts, source uncertainty and currency-separated archive summaries."""
from datetime import date, datetime, timezone
from decimal import Decimal
import unittest
from uuid import uuid4

from pydantic import ValidationError

from inventory_hub.services import invoice_finance as finance


def invoice(**changes):
    return {"id": 1, "currency": "EUR", "total_amount": Decimal("100"),
        "total_with_vat": Decimal("100"), "vat_rate": Decimal("0"), "vat_included": True,
        "source_paid_amount": Decimal("0"), "payment_revision": 0,
        "invoice_date": date(2026, 9, 1), "due_date": date(2026, 9, 20), **changes}


class InvoiceFinanceTests(unittest.TestCase):
    def test_invoice_header_sort_uses_displayed_decimal_snapshots_and_unknowns_last(self):
        rows = [{"id": key} for key in (7, 3, 5, 1)]
        finances = {
            7: {"gross_amount": None, "outstanding_amount": None, "status": "unknown"},
            3: {"gross_amount": "9007199254740993.01", "outstanding_amount": "0.00", "status": "paid"},
            5: {"gross_amount": "9007199254740993.00", "outstanding_amount": "1.01", "status": "partial"},
            1: {"gross_amount": "9007199254740993.00", "outstanding_amount": "3.00", "status": "unpaid"},
        }
        self.assertEqual(finance.sorted_invoice_ids(rows, finances, sort_by="total_amount"), [1, 5, 3, 7])
        self.assertEqual(finance.sorted_invoice_ids(rows, finances, sort_by="total_amount", descending=True), [3, 1, 5, 7])
        self.assertEqual(finance.sorted_invoice_ids(rows, finances, sort_by="outstanding_amount"), [3, 5, 1, 7])
        self.assertEqual(finance.sorted_invoice_ids(rows, finances, sort_by="outstanding_amount", descending=True), [1, 5, 3, 7])
        self.assertEqual(finance.sorted_invoice_ids(rows, finances, sort_by="payment_status"), [1, 5, 3, 7])
        self.assertEqual(finance.sorted_invoice_ids(rows, finances, sort_by="payment_status", descending=True), [3, 5, 1, 7])

    def test_exact_tax_basis_rounding_and_zero_rate(self):
        self.assertEqual(finance.derived_totals(Decimal("123"), Decimal("23"), True),
            {"total_without_vat": Decimal("100"), "vat_amount": Decimal("23"), "total_with_vat": Decimal("123")})
        self.assertEqual(finance.derived_totals(Decimal("0.05"), Decimal("10"), False),
            {"total_without_vat": Decimal("0.05"), "vat_amount": Decimal("0.01"), "total_with_vat": Decimal("0.06")})
        self.assertEqual(finance.derived_totals(Decimal("-0.05"), Decimal("10"), False)["total_with_vat"], Decimal("-0.06"))
        self.assertEqual(finance.derived_totals(Decimal("0"), Decimal("0"), True),
            {"total_without_vat": Decimal("0"), "vat_amount": Decimal("0"), "total_with_vat": Decimal("0")})

    def test_unknown_vat_is_not_defaulted_and_unknown_amount_is_not_zero(self):
        self.assertEqual(finance.derived_totals(None, Decimal("23"), True),
            {"total_without_vat": None, "vat_amount": None, "total_with_vat": None})
        self.assertIsNone(finance.derived_totals(Decimal("100"), None, False)["total_with_vat"])
        self.assertEqual(finance.derived_totals(Decimal("100"), None, True)["total_with_vat"], Decimal("100"))
        self.assertIsNone(finance.derived_totals(Decimal("100"), Decimal("23"), None)["total_with_vat"])

    def test_currency_totals_do_not_add_euro_and_czk_and_partial_is_remaining(self):
        rows = [invoice(source_paid_amount=Decimal("30")),
                invoice(id=2, currency="CZK", total_with_vat=Decimal("2500"), source_paid_amount=Decimal("500"))]
        result = finance.summarize(rows, today=date(2026, 10, 1))
        self.assertIsNone(result["total_amount"])
        self.assertIsNone(result["currency"])
        groups = {item["currency"]: item for item in result["totals_by_currency"]}
        self.assertEqual((groups["EUR"]["total_amount"], groups["EUR"]["outstanding_amount"], groups["EUR"]["overdue_amount"]),
                         ("100.00", "70.00", "70.00"))
        self.assertEqual(groups["CZK"]["outstanding_amount"], "2000.00")
        self.assertEqual(result["unpaid_count"], 2)
        self.assertEqual(result["overdue_count"], 2)
        self.assertEqual({(item["currency"], item["total_amount"]) for item in result["monthly"]},
                         {("EUR", "100.00"), ("CZK", "2500.00")})

    def test_unknown_paid_legacy_flag_never_becomes_zero_or_full_settlement(self):
        for status in ("unpaid", "partial", "paid"):
            with self.subTest(status=status):
                result = finance.snapshot(invoice(source_paid_amount=None, paid_amount=None,
                    payment_status=status), today=date(2026, 10, 1))
                self.assertEqual(result["status"], "unknown")
                self.assertIsNone(result["paid_amount"])
                self.assertIsNone(result["outstanding_amount"])
                self.assertTrue(result["opening_required"])
                self.assertFalse(result["is_overdue"])
        explicit = finance.snapshot(invoice(source_paid_amount=None, source_payment_status="unpaid"))
        self.assertEqual(explicit["paid_amount"], "0.00")
        self.assertEqual(explicit["status"], "unpaid")
        ambiguous = finance.snapshot(invoice(source_paid_amount=None, source_payment_status="not_paid"))
        self.assertIsNone(ambiguous["paid_amount"])

    def test_ledger_overrides_later_source_status_and_resolves_partial_balance(self):
        row = invoice(source_paid_amount=Decimal("100"), source_payment_status="paid",
            ledger_count=2, ledger_paid_amount=Decimal("35"), ledger_currency="EUR")
        result = finance.snapshot(row, today=date(2026, 10, 1))
        self.assertEqual((result["paid_amount"], result["outstanding_amount"], result["status"]),
                         ("35.00", "65.00", "partial"))
        self.assertFalse(result["opening_required"])
        self.assertTrue(result["is_overdue"])

    def test_signed_credit_note_remains_separate_and_has_signed_remaining(self):
        result = finance.snapshot(invoice(total_with_vat=Decimal("-100"), source_paid_amount=Decimal("-25")))
        self.assertEqual((result["paid_amount"], result["outstanding_amount"], result["status"]),
                         ("-25.00", "-75.00", "partial"))
        self.assertTrue(finance.balance_valid(Decimal("-100"), Decimal("-25")))
        self.assertFalse(finance.balance_valid(Decimal("-100"), Decimal("25")))
        self.assertFalse(finance.balance_valid(Decimal("-100"), Decimal("-101")))

    def test_positive_credit_source_reduces_cost_without_rewriting_original_amounts(self):
        credit = invoice(id=2, document_type="credit_note", total_with_vat=Decimal("20"), source_paid_amount=Decimal("5"))
        result = finance.snapshot(credit)
        self.assertEqual((result["gross_amount"], result["paid_amount"], result["outstanding_amount"]),
                         ("-20.00", "-5.00", "-15.00"))
        self.assertEqual(credit["total_with_vat"], Decimal("20"))
        self.assertEqual(credit["source_paid_amount"], Decimal("5"))
        summary = finance.summarize([invoice(), credit])
        self.assertEqual((summary["total_amount"], summary["unpaid_amount"]), ("80.00", "85.00"))

    def test_zero_preserved_and_unknown_totals_mark_aggregation_incomplete(self):
        zero = finance.snapshot(invoice(total_with_vat=Decimal("0")))
        self.assertEqual((zero["gross_amount"], zero["paid_amount"], zero["outstanding_amount"], zero["status"]),
                         ("0.00", "0.00", "0.00", "paid"))
        result = finance.summarize([invoice(), invoice(id=2, total_amount=None, total_with_vat=None)], today=date(2026, 10, 1))
        self.assertIsNone(result["total_amount"])
        self.assertIsNone(result["unpaid_amount"])
        self.assertEqual(result["totals_by_currency"][0]["unknown_amount_count"], 1)
        self.assertEqual(result["totals_by_currency"][0]["unknown_outstanding_count"], 1)
        self.assertIsNone(result["overdue_amount"])

    def test_impossible_source_balance_and_changed_ledger_currency_are_visible(self):
        overpaid = finance.snapshot(invoice(source_paid_amount=Decimal("101")))
        self.assertEqual(overpaid["status"], "unknown")
        self.assertIn("invoice_payment_balance_conflict", overpaid["issues"])
        mismatch = finance.snapshot(invoice(ledger_count=1, ledger_paid_amount=Decimal("20"), ledger_currency="CZK"))
        self.assertEqual(mismatch["status"], "unknown")
        self.assertIn("invoice_payment_currency_conflict", mismatch["issues"])

    def test_payment_input_requires_exact_positive_money_and_explicit_time(self):
        base = {"request_id": str(uuid4()), "expected_revision": 0, "kind": "payment",
                "amount": "10.01", "paid_at": "2026-10-01T10:00:00Z"}
        self.assertEqual(finance.PaymentRequest.model_validate(base).amount, "10.01")
        for amount in (10.01, True, "", "NaN", "Infinity", "0", "-1", "0.001", "1000000000000"):
            with self.subTest(amount=amount), self.assertRaises(ValidationError):
                finance.PaymentRequest.model_validate({**base, "amount": amount})
        with self.assertRaises(ValidationError):
            finance.PaymentRequest.model_validate({**base, "paid_at": "2026-10-01T10:00:00"})
        with self.assertRaises(ValidationError):
            finance.PaymentRequest.model_validate({**base, "expected_revision": True})


if __name__ == "__main__":
    unittest.main()
