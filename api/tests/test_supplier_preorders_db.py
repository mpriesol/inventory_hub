"""Verify that documented expected deliveries only change with posted receipts."""
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import unittest
from uuid import uuid4

from sqlalchemy import text

from invoice_db_fixture import DATABASE_REQUIRED, TEST_URL, InvoiceDatabaseCase
from inventory_hub.services import invoice_archive, supplier_preorders as service


@unittest.skipUnless(TEST_URL, DATABASE_REQUIRED)
class SupplierPreordersDatabaseTests(InvoiceDatabaseCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        sql = Path(__file__).resolve().parents[2] / "infra" / "db-init" / "061_supplier_preorders.sql"
        async with self.engine.begin() as conn:
            raw = await conn.get_raw_connection()
            await raw.driver_connection.execute(sql.read_text())

    async def test_two_invoices_add_only_posted_quantities_and_keep_history(self):
        request = service.PreorderCreate(request_id=uuid4(), supplier_id=self.supplier_id,
            reference="PO-TEST", ordered_on=date(2026, 1, 1), approved=True,
            lines=[{"sku": "ARCHIVE-1", "ordered_quantity": "10", "expected_on": "2026-09-01"}])
        async with self.sessions() as db:
            created = await service.create(db, request, actor="fixture")
            self.assertEqual(created["lines"][0]["remaining_quantity"], 10)
            await db.commit()
        await self.assert_no_inventory_effects()
        async with self.sessions() as db:
            self.assertEqual((await service.create(db, request, actor="fixture"))["id"], request.request_id)
            await db.commit()

        for index, quantity, remaining in ((1, 4, 6), (2, 6, 0)):
            key = f"TEST-SHIPMENT-{index}"
            async with self.sessions() as db:
                invoice = await invoice_archive.ingest_invoice(db, direction="purchase",
                    source_kind="supplier", source_key=key,
                    metadata={"invoice_number": key, "invoice_date": f"2026-09-0{index}"},
                    supplier_id=self.supplier_id)
                order = await service.link_invoice(db, request.request_id, invoice["id"], actor="fixture")
                self.assertEqual(order["lines"][0]["remaining_quantity"], remaining + quantity)
                warehouse = await db.scalar(text("SELECT id FROM warehouses WHERE is_default LIMIT 1"))
                receipt = await db.scalar(text("""INSERT INTO receiving_sessions
                    (warehouse_id,supplier_id,invoice_number,status) VALUES(:warehouse,:supplier,:number,'in_progress') RETURNING id"""),
                    {"warehouse": warehouse, "supplier": self.supplier_id, "number": key})
                movement = text("""INSERT INTO stock_movements(idempotency_key,product_id,warehouse_id,movement_type,
                    quantity,reference_type,reference_id,balance_after,avg_cost_after,created_at)
                    VALUES(:key,:product,:warehouse,'RECEIVING_IN',:quantity,'receiving_session',:receipt,:balance,0,
                        CAST(:day AS timestamptz)) RETURNING id""")
                values = {"key": key, "product": self.product_id, "warehouse": warehouse,
                          "quantity": quantity, "receipt": str(receipt), "balance": 10 - remaining,
                          "day": datetime(2026, 9, index, 12, tzinfo=timezone.utc)}
                posted = await db.scalar(movement, values)
                if index == 1:
                    # A compensated receiving movement stays in the ledger. The
                    # receipt's replacement alone contributes to the preorder.
                    await db.execute(text("""INSERT INTO fifo_layers(product_id,warehouse_id,receipt_movement_id,
                        physical_received_at,quantity_original,quantity_remaining,cost_status,provenance)
                        VALUES(:product,:warehouse,:movement,:day,:quantity,0,'unknown',
                            jsonb_build_object('receiving_reversal_id',CAST(:operation AS text)))"""),
                        {**values, "movement": posted, "operation": str(uuid4())})
                    await db.execute(movement, {**values, "key": f"{key}:r1", "day": values["day"].replace(hour=13)})
                order = await service.detail(db, request.request_id)
                self.assertEqual(order["lines"][0]["remaining_quantity"], Decimal(remaining))
                await db.commit()
        self.assertEqual(order["status"], "completed")
        self.assertEqual(len(order["invoices"]), 2)
        self.assertEqual([row["quantity"] for row in order["lines"][0]["deliveries"]], [4, 6])
        self.assertEqual([row["received_at"].date().isoformat() for row in order["lines"][0]["deliveries"]],
                         ["2026-09-01", "2026-09-02"])
        self.assertEqual(order["lines"][0]["deliveries"][0]["received_at"].hour, 13)
