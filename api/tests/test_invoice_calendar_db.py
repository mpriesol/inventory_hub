"""Due dates and current payment evidence across the complete selected period."""
from datetime import date
import unittest
from uuid import uuid4
from unittest.mock import patch

from sqlalchemy import text

from invoice_db_fixture import DATABASE_REQUIRED, TEST_URL, InvoiceDatabaseCase
from inventory_hub.services import invoice_archive as archive
from inventory_hub.services import invoice_calendar as service


@unittest.skipUnless(TEST_URL, DATABASE_REQUIRED)
class InvoiceCalendarDatabaseTests(InvoiceDatabaseCase):
    async def invoice(self, *, supplier_id=None, direction="purchase", **metadata):
        source = {"direction": direction, "source_kind": "supplier" if direction == "purchase" else "shop",
            "source_key": uuid4().hex, "metadata": {"invoice_number": "CAL-" + uuid4().hex,
                "invoice_date": "2024-01-15", "due_date": "2024-02-29", "currency": "EUR",
                "total_with_vat": "123.45", "source_paid_amount": "0.00", **metadata}}
        source["supplier_id" if direction == "purchase" else "shop_id"] = (supplier_id or self.supplier_id) if direction == "purchase" else self.shop_id
        async with self.sessions() as db:
            result = await archive.ingest_invoice(db, **source)
            await db.commit()
        return result["id"]

    async def read(self, **changes):
        async with self.sessions() as db:
            return await service.calendar(db, {"range_from": "2024-02-01", "range_to": "2024-03-31",
                "supplier_code": "archive-fixture", **changes}, today=date(2024, 3, 5))

    async def test_exact_owner_period_and_payment_filters_keep_unknown_separate_and_do_not_write_inventory(self):
        unpaid = await self.invoice(invoice_number="SAME-NUMBER")
        unknown = await self.invoice(invoice_date="2024-02-15", currency="CZK", source_paid_amount=None)
        paid = await self.invoice(due_date="2024-03-01", source_paid_amount="123.45")
        await self.invoice(supplier_id=self.other_supplier_id, invoice_number="SAME-NUMBER")
        await self.invoice(direction="sale")
        await self.invoice(due_date=None)
        result = await self.read()
        self.assertEqual([row["id"] for row in result["items"]], [unpaid, unknown, paid])
        self.assertEqual(result["items"][0]["gross_amount"], "123.45")
        self.assertTrue(result["items"][0]["is_overdue"])
        self.assertEqual(result["items"][1]["payment_status"], "unknown")
        self.assertFalse(result["items"][1]["is_overdue"])
        self.assertIsNone(result["items"][1]["outstanding_amount"])
        self.assertEqual(result["items"][1]["currency"], "CZK")
        self.assertEqual((await self.read(payment_status="unknown"))["items"][0]["id"], unknown)
        self.assertEqual((await self.read(payment_status="paid"))["items"][0]["id"], paid)
        self.assertEqual([row["id"] for row in (await self.read(date_from="2024-02-01", date_to="2024-02-29"))["items"]], [unknown])
        self.assertEqual([row["id"] for row in (await self.read(is_overdue=True))["items"]], [unpaid])
        self.assertEqual((await self.read(direction="sale", supplier_code=None))["total"], 1)
        await self.assert_no_inventory_effects()

    async def test_calendar_contains_more_than_the_invoice_table_page(self):
        ids = [await self.invoice(invoice_number=f"CAL-{index}") for index in range(31)]
        result = await self.read()
        self.assertEqual(result["total"], 31)
        self.assertEqual([row["id"] for row in result["items"]], ids)
        self.assertEqual(result["items"][-1]["due_date"], "2024-02-29")
        await self.assert_no_inventory_effects()

    async def test_due_date_edits_between_reads_do_not_crash_or_leak_outside_the_selected_period(self):
        removed = await self.invoice()
        moved = await self.invoice()
        original = service.finance.financial_rows

        async def edit_before_finance(db, **arguments):
            async with self.sessions() as other:
                await other.execute(text("UPDATE uploaded_invoices SET due_date=NULL WHERE id=:id"), {"id": removed})
                await other.execute(text("UPDATE uploaded_invoices SET due_date='2024-04-01' WHERE id=:id"), {"id": moved})
                await other.commit()
            return await original(db, **arguments)

        with patch.object(service.finance, "financial_rows", side_effect=edit_before_finance):
            result = await self.read()
        self.assertEqual((result["items"], result["total"]), ([], 0))
        await self.assert_no_inventory_effects()
