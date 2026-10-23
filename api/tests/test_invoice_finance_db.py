"""Settlement ledger replay, concurrent edits and signed documents in PostgreSQL."""
import asyncio
from datetime import date
from decimal import Decimal as D
import unittest
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from invoice_db_fixture import DATABASE_REQUIRED, TEST_URL, InvoiceDatabaseCase
from inventory_hub.services import invoice_archive as archive
from inventory_hub.services import invoice_finance as finance


@unittest.skipUnless(TEST_URL, DATABASE_REQUIRED)
class InvoiceFinanceDatabaseTests(InvoiceDatabaseCase):
    async def invoice(self, *, gross="100.00", paid="0.00", currency="EUR", source_kind="supplier", **metadata):
        source = {"direction": "purchase", "source_kind": source_kind, "supplier_id": self.supplier_id,
                  "source_key": str(uuid4()), "metadata": {
                      "invoice_number": "FIN-" + uuid4().hex, "currency": currency,
                      "invoice_date": "2026-01-01", "due_date": "2026-01-15",
                      "total_with_vat": gross, "source_paid_amount": paid, **metadata}}
        async with self.sessions() as db:
            result = await archive.ingest_invoice(db, **source)
            await db.commit()
        return result["id"], source

    def request(self, amount="30.00", **changes):
        return finance.PaymentRequest.model_validate({
            "request_id": str(uuid4()), "expected_revision": 0, "amount": amount,
            "kind": "payment", "paid_at": "2026-01-10T12:30:00+01:00", "note": "Synthetic bank entry", **changes,
        })

    async def apply(self, invoice_id, request, *, reverse=None):
        async with self.sessions() as db:
            result = await finance.apply_payment(db, invoice_id, request, actor="fixture-operator",
                                                 reverses_payment_id=reverse)
            await db.commit()
            return result

    async def current(self, invoice_id):
        async with self.sessions() as db:
            row = (await db.execute(text("SELECT * FROM uploaded_invoices WHERE id=:id"),
                                    {"id": invoice_id})).mappings().one()
            return await finance.detail(db, row)

    async def metadata(self, invoice_id, changes, revision):
        async with self.sessions() as db:
            result = await finance.update_metadata(db, invoice_id, changes, revision)
            await db.commit()
            return result

    async def expect_error(self, code, invoice_id, request, *, reverse=None):
        with self.assertRaises(finance.FinanceError) as error:
            await self.apply(invoice_id, request, reverse=reverse)
        self.assertEqual(error.exception.code, code)

    async def test_partial_payment_freezes_explicit_source_opening_and_keeps_exact_cents(self):
        invoice_id, _ = await self.invoice(gross="100.01", paid="20.00")
        result = await self.apply(invoice_id, self.request("30.01"))
        self.assertEqual((result["payment_revision"], result["finance"]["paid_amount"],
                          result["finance"]["outstanding_amount"], result["finance"]["status"]),
                         (1, "50.01", "50.00", "partial"))
        ledger = await self.rows("SELECT kind,amount,currency FROM invoice_payments ORDER BY id")
        self.assertEqual([tuple(row.values()) for row in ledger],
                         [("opening", D("20.00"), "EUR"), ("payment", D("30.01"), "EUR")])
        await self.assert_no_inventory_effects()

    async def test_unknown_legacy_paid_status_needs_explicit_opening_before_manual_payment(self):
        invoice_id, _ = await self.invoice(paid=None, source_payment_status="paid")
        current = await self.current(invoice_id)
        self.assertEqual((current["paid_amount"], current["outstanding_amount"], current["status"],
                          current["opening_required"]), (None, None, "unknown", True))
        await self.expect_error("invoice_payment_opening_required", invoice_id, self.request())
        self.assertEqual(await self.scalar("SELECT count(*) FROM invoice_payments"), 0)
        self.assertEqual(await self.scalar("SELECT count(*) FROM invoice_payment_operations"), 0)
        result = await self.apply(invoice_id, self.request(opening_paid_amount="20.00"))
        self.assertEqual(result["finance"]["paid_amount"], "50.00")
        await self.expect_error("invoice_payment_opening_already_recorded", invoice_id,
                                self.request("10.00", expected_revision=1, opening_paid_amount="20.00"))

    async def test_source_refresh_cannot_change_the_manual_ledger_opening(self):
        invoice_id, source = await self.invoice(paid="20.00")
        first = await self.apply(invoice_id, self.request())
        source["metadata"].update(source_payment_status="paid", source_paid_amount="100.00")
        async with self.sessions() as db:
            replay = await archive.ingest_invoice(db, **source)
            await db.commit()
        self.assertTrue(replay["replayed"])
        current = await self.current(invoice_id)
        self.assertEqual((current["paid_amount"], current["outstanding_amount"], current["payment_revision"]),
                         ("50.00", "50.00", 1))
        self.assertEqual(current["payments"], first["finance"]["payments"])
        refund = await self.apply(invoice_id, self.request("10.00", kind="refund", expected_revision=1))
        self.assertEqual((refund["finance"]["paid_amount"], refund["finance"]["outstanding_amount"]), ("40.00", "60.00"))

    async def test_changed_source_opening_invalidates_stale_manual_payment_preview(self):
        invoice_id, source = await self.invoice()
        source["metadata"]["source_paid_amount"] = "20.00"
        async with self.sessions() as db:
            await archive.ingest_invoice(db, **source)
            await db.commit()
        await self.expect_error("invoice_payment_revision_changed", invoice_id, self.request())
        self.assertEqual(await self.scalar("SELECT count(*) FROM invoice_payments"), 0)
        applied = await self.apply(invoice_id, self.request(expected_revision=1))
        self.assertEqual((applied["finance"]["paid_amount"], applied["payment_revision"]), ("50.00", 2))

    async def test_metadata_and_payments_share_revision_and_financial_basis_is_locked(self):
        invoice_id, _ = await self.invoice(source_kind="upload")
        updated = await self.metadata(invoice_id, {"notes": "Confirmed supplier reference"}, 0)
        self.assertEqual(updated["payment_revision"], 1)
        await self.expect_error("invoice_payment_revision_changed", invoice_id, self.request())
        await self.apply(invoice_id, self.request(expected_revision=1))
        for changes in ({"currency": "CZK"}, {"total_amount": D("90.00")}, {"vat_rate": D("19.00")}):
            with self.subTest(changes=changes), self.assertRaises(finance.FinanceError) as error:
                await self.metadata(invoice_id, changes, 2)
            self.assertEqual(error.exception.code, "invoice_payment_financial_basis_locked")
        self.assertEqual((await self.current(invoice_id))["payment_revision"], 2)
        self.assertEqual((await self.current(invoice_id))["paid_amount"], "30.00")

    async def test_supplier_and_shop_commercial_facts_stay_read_only_after_ingest(self):
        supplier_invoice, source = await self.invoice()
        source.update(direction="sale", source_kind="shop", supplier_id=None, shop_id=self.shop_id)
        async with self.sessions() as db:
            sale = await archive.ingest_invoice(db, **source)
            await db.commit()
        for invoice_id in (supplier_invoice, sale["id"]):
            for changes in ({"invoice_number": "REPLACED"}, {"currency": "CZK"}, {"total_amount": D("1")}):
                with self.subTest(invoice_id=invoice_id, changes=changes), self.assertRaises(finance.FinanceError) as error:
                    await self.metadata(invoice_id, changes, 0)
                self.assertEqual(error.exception.code, "invoice_source_facts_read_only")
            self.assertEqual((await self.current(invoice_id))["payment_revision"], 0)
        await self.assert_no_inventory_effects()

    async def test_request_replay_recovery_and_cross_invoice_payload_conflicts(self):
        invoice_id, _ = await self.invoice()
        other_id, _ = await self.invoice()
        request = self.request()
        first = await self.apply(invoice_id, request)
        await self.apply(invoice_id, self.request("10.00", expected_revision=1))
        replay = await self.apply(invoice_id, request)
        self.assertEqual(replay, first)
        async with self.sessions() as db:
            self.assertEqual(await finance.operation_result(db, invoice_id, request.request_id), first)
            with self.assertRaises(finance.FinanceError) as error:
                await finance.operation_result(db, other_id, request.request_id)
            self.assertEqual(error.exception.code, "invoice_payment_operation_not_found")
        for changed in (
            request.model_copy(update={"amount": "31.00"}),
            request.model_copy(update={"expected_revision": 2}),
        ):
            await self.expect_error("invoice_payment_request_conflict", invoice_id, changed)
        await self.expect_error("invoice_payment_request_conflict", other_id, request)
        self.assertEqual(await self.scalar("SELECT count(*) FROM invoice_payment_operations"), 2)
        self.assertEqual(await self.scalar("SELECT count(*) FROM invoice_payments"), 3)
        self.assertEqual((await self.current(invoice_id))["paid_amount"], "40.00")

    async def test_concurrent_identical_request_records_only_one_payment(self):
        invoice_id, _ = await self.invoice()
        request = self.request()
        first, second = await asyncio.gather(self.apply(invoice_id, request), self.apply(invoice_id, request))
        self.assertEqual(first, second)
        self.assertEqual(await self.scalar("SELECT count(*) FROM invoice_payments"), 2)
        self.assertEqual(await self.scalar("SELECT count(*) FROM invoice_payment_operations"), 1)

    async def test_concurrent_distinct_requests_from_same_revision_have_one_winner(self):
        invoice_id, _ = await self.invoice()
        outcomes = await asyncio.gather(self.apply(invoice_id, self.request("30.00")),
                                        self.apply(invoice_id, self.request("40.00")), return_exceptions=True)
        accepted = [item for item in outcomes if isinstance(item, dict)]
        rejected = [item for item in outcomes if isinstance(item, finance.FinanceError)]
        self.assertEqual((len(accepted), len(rejected)), (1, 1), outcomes)
        self.assertEqual(rejected[0].code, "invoice_payment_revision_changed")
        self.assertEqual(await self.scalar("SELECT count(*) FROM invoice_payments"), 2)
        self.assertEqual(await self.scalar("SELECT count(*) FROM invoice_payment_operations"), 1)
        self.assertEqual((await self.current(invoice_id))["payment_revision"], 1)

    async def test_rolled_back_request_can_be_retried_without_partial_opening_or_receipt(self):
        invoice_id, _ = await self.invoice(paid="10.00")
        request = self.request()
        async with self.sessions() as db:
            await finance.apply_payment(db, invoice_id, request, actor="fixture-operator")
            await db.rollback()
        self.assertEqual(await self.scalar("SELECT count(*) FROM invoice_payments"), 0)
        self.assertEqual(await self.scalar("SELECT count(*) FROM invoice_payment_operations"), 0)
        self.assertEqual((await self.current(invoice_id))["payment_revision"], 0)
        result = await self.apply(invoice_id, request)
        self.assertEqual(result["finance"]["paid_amount"], "40.00")

    async def test_refund_and_reversal_preserve_entries_and_cannot_invent_settlement(self):
        invoice_id, _ = await self.invoice()
        await self.expect_error("invoice_payment_amount_exceeds_balance", invoice_id,
                                self.request("1.00", kind="refund"))
        first = await self.apply(invoice_id, self.request("50.00"))
        payment_id = first["finance"]["payments"][-1]["id"]
        original = dict((await self.rows("SELECT * FROM invoice_payments WHERE id=:id", {"id": payment_id}))[0])
        refund = await self.apply(invoice_id, self.request("20.00", kind="refund", expected_revision=1))
        refund_id = refund["finance"]["payments"][-1]["id"]
        reversal = lambda revision: finance.ReversalRequest(request_id=uuid4(), expected_revision=revision,
                                                            note="Correct synthetic posting")
        await self.expect_error("invoice_payment_amount_exceeds_balance", invoice_id, reversal(2), reverse=payment_id)
        reversed_refund = await self.apply(invoice_id, reversal(2), reverse=refund_id)
        self.assertEqual(reversed_refund["finance"]["paid_amount"], "50.00")
        reversed_payment = await self.apply(invoice_id, reversal(3), reverse=payment_id)
        self.assertEqual((reversed_payment["finance"]["paid_amount"], reversed_payment["finance"]["status"]),
                         ("0.00", "unpaid"))
        self.assertEqual(dict((await self.rows("SELECT * FROM invoice_payments WHERE id=:id", {"id": payment_id}))[0]), original)
        await self.expect_error("invoice_payment_not_reversible", invoice_id, reversal(4), reverse=payment_id)
        opening_id = first["finance"]["payments"][0]["id"]
        await self.expect_error("invoice_payment_not_reversible", invoice_id, reversal(4), reverse=opening_id)
        await self.assert_no_inventory_effects()

    async def test_credit_note_settlement_is_signed_and_never_offsets_other_invoices(self):
        credit_id, _ = await self.invoice(gross="-100.00", document_type="credit_note")
        other_id, _ = await self.invoice()
        first = await self.apply(credit_id, self.request("40.00"))
        self.assertEqual((first["finance"]["paid_amount"], first["finance"]["outstanding_amount"]),
                         ("-40.00", "-60.00"))
        refund = await self.apply(credit_id, self.request("10.00", kind="refund", expected_revision=1))
        self.assertEqual((refund["finance"]["paid_amount"], refund["finance"]["outstanding_amount"]),
                         ("-30.00", "-70.00"))
        await self.expect_error("invoice_payment_amount_exceeds_balance", credit_id,
                                self.request("70.01", expected_revision=2))
        self.assertEqual((await self.current(other_id))["paid_amount"], "0.00")
        await self.assert_no_inventory_effects()

    async def test_positive_source_credit_totals_have_negative_finance_without_rewriting_issuer_facts(self):
        invoice_id, _ = await self.invoice(gross="100.00", paid="20.00", document_type="credit_note")
        before = await self.current(invoice_id)
        self.assertEqual((before["gross_amount"], before["paid_amount"], before["outstanding_amount"]),
                         ("-100.00", "-20.00", "-80.00"))
        result = await self.apply(invoice_id, self.request("30.00"))
        self.assertEqual((result["finance"]["paid_amount"], result["finance"]["outstanding_amount"]),
                         ("-50.00", "-50.00"))
        original = (await self.rows("SELECT total_with_vat,source_paid_amount FROM uploaded_invoices WHERE id=:id",
                                    {"id": invoice_id}))[0]
        self.assertEqual(dict(original), {"total_with_vat": D("100.00"), "source_paid_amount": D("20.00")})
        async with self.sessions() as db:
            totals = finance.summarize(await finance.financial_rows(db))
        self.assertEqual((totals["total_amount"], totals["unpaid_total"]), ("-100.00", "-50.00"))
        await self.assert_no_inventory_effects()

    async def test_database_prevents_ledger_update_and_delete(self):
        invoice_id, _ = await self.invoice()
        await self.apply(invoice_id, self.request())
        for sql in ("UPDATE invoice_payments SET amount=99 WHERE kind='payment'", "DELETE FROM invoice_payments"):
            async with self.sessions() as db:
                with self.assertRaises(DBAPIError) as error:
                    await db.execute(text(sql))
                self.assertIn("invoice_payment_immutable", str(error.exception))
        self.assertEqual((await self.current(invoice_id))["paid_amount"], "30.00")

    async def test_unknown_totals_and_currency_block_payment_without_rows(self):
        no_total, _ = await self.invoice(gross=None)
        no_currency, _ = await self.invoice(currency=None)
        await self.expect_error("invoice_total_unknown", no_total, self.request())
        await self.expect_error("invoice_currency_unknown", no_currency, self.request())
        self.assertEqual(await self.scalar("SELECT count(*) FROM invoice_payments"), 0)

    async def test_currency_reporting_separates_money_and_uses_remaining_balance(self):
        euro, _ = await self.invoice(gross="100.00", paid="20.00")
        await self.apply(euro, self.request("30.00"))
        await self.invoice(gross="200.00", paid="0.00", currency="CZK")
        await self.invoice(gross="-10.00", paid="0.00", document_type="credit_note")
        async with self.sessions() as db:
            result = finance.summarize(await finance.financial_rows(db), today=date(2026, 2, 1))
        self.assertEqual((result["currency"], result["total_amount"], result["unpaid_total"]), (None, None, None))
        groups = {row["currency"]: row for row in result["totals_by_currency"]}
        self.assertEqual((groups["EUR"]["total_amount"], groups["EUR"]["outstanding_amount"]), ("90.00", "40.00"))
        self.assertEqual((groups["CZK"]["total_amount"], groups["CZK"]["outstanding_amount"]), ("200.00", "200.00"))
        self.assertEqual({row["currency"] for row in result["monthly"]}, {"EUR", "CZK"})
        self.assertEqual({row["currency"] for row in result["aging"]}, {"EUR", "CZK"})
