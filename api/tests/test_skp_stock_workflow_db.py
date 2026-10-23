"""SKP invoice CSVs reuse receiving identity, FIFO and immutable recounts."""
import json
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import asyncpg
from sqlalchemy import func, select

import test_receiving_db as fixture
from inventory_hub import config_io
from inventory_hub.adapters.sk_profi_bike_invoice import Invoice, InvoiceLine
from inventory_hub.db_models import IdentifierType, MovementType, Product, ProductIdentifier, Supplier
from inventory_hub.db_models_ext import ProductSupplySource, StockBalance, StockMovement
from inventory_hub.fifo_models import FifoAllocation, FifoLayer
from inventory_hub.routers import receiving_db as receiving
from inventory_hub.services import stock_adjustments, product_editor
from inventory_hub.product_editor_types import ProductCardCreateRequest
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.stock_adjustment_types import StockAdjustmentApply, StockAdjustmentPreview


@unittest.skipUnless(fixture.TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class SkpStockWorkflowDatabaseTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixture.ReceivingDatabaseTests.asyncTearDown

    async def asyncSetUp(self):
        await fixture.ReceivingDatabaseTests.asyncSetUp(self)
        connection = await asyncpg.connect(fixture.TEST_URL)
        try:
            await connection.execute(f'SET search_path TO "{self.schema}"')
            root = Path(__file__).resolve().parents[2] / "infra/db-init"
            for name in ("004_shop_product_content.sql", "012_product_editor.sql", "014_supplier_availability.sql",
                         "016_product_publication.sql", "017_stock_adjustments.sql", "018_fifo_cost_sync.sql", "029_sku_policy.sql"):
                await connection.execute((root / name).read_text())
        finally:
            await connection.close()
        # Keep all filesystem effects in the receiving fixture's temporary root.
        # Prefix loading itself uses the normal supplier configuration.
        self.prefix_patch.stop()
        config_path = config_io.supplier_path("sk-profi-bike")
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({"product_code_prefix": "SKP-"}))
        async with self.sessions() as db:
            supplier = Supplier(code="sk-profi-bike", name="SK Profi Bike")
            db.add(supplier)
            product = await db.get(Product, self.product_id)
            product.sku = "PL-existing-part"
            product.supplier_id = self.supplier_id
            await ProductIdentifierService(db).add_identifier(product.id, "4006381333931")
            await db.commit()
            self.skp_id = supplier.id

    async def receive_csv(self, number, content, *, scan_quantity="1"):
        path = Path(self.config_folder.name) / f"{number}.csv"
        path.write_bytes(content)
        with patch.object(receiving, "_invoice_csv_path", return_value=path):
            async with self.sessions() as db:
                prepared = await receiving.create_session("sk-profi-bike", receiving.CreateSessionRequest(
                    invoice_id=f"sk-profi-bike:{number}", warehouse_code="receipt-test"), db)
                await db.commit()
        # Opening an invoice is not evidence that the goods physically arrived.
        async with self.sessions() as db:
            before_count = await db.scalar(select(func.count()).select_from(StockMovement))
            scan = receiving.ScanRequest(code="4006381333931", qty=scan_quantity, request_id=uuid4())
            scanned = await receiving.scan_code("sk-profi-bike", prepared["session_id"], scan, db)
            await db.commit()
            self.assertEqual(scanned["status"], "matched")
            self.assertEqual(scanned["line"]["product_code"], "PL-existing-part")
        async with self.sessions() as db:
            replay = await receiving.scan_code("sk-profi-bike", prepared["session_id"], scan, db)
            self.assertTrue(replay["replayed"])
            self.assertEqual(replay["line"]["received_qty"], scanned["line"]["received_qty"])
            await receiving.accept_all_items("sk-profi-bike", prepared["session_id"],
                                             receiving.AcceptAllRequest(), db)
            await db.commit()
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)), before_count)
        async with self.sessions() as db:
            result = await receiving.finalize_session("sk-profi-bike", prepared["session_id"],
                                                       receiving.FinalizeRequest(), db)
        async with self.sessions() as db:
            replay = await receiving.finalize_session("sk-profi-bike", prepared["session_id"],
                                                       receiving.FinalizeRequest(), db)
        self.assertEqual(result, replay)
        return prepared, result

    async def test_invoice_receiving_and_recount_keep_shared_code_costs_and_retries(self):
        # A verified EAN from canonical CSV connects a different supplier's SKU
        # to the existing product instead of inventing another physical item.
        prepared, first = await self.receive_csv("TEST-EAN", (
            "SCM;TITLE;QTY;PRICE;EAN\n"
            "shared-part;Synthetic shared part;2;10;4006381333931\n"
        ).encode(), scan_quantity="2")
        self.assertEqual(prepared["lines"][0]["product_code"], "PL-existing-part")
        self.assertEqual((first["products_created"], first["movements_created"]), (0, 1))

        # New products are prepared independently from confirming physical units.
        # The registered manufacturer adds its prefix to the confirmed product
        # code. Creating a card still does not confirm physical stock.
        async with self.sessions() as db:
            new_card = await product_editor.create_card(db, ProductCardCreateRequest(request_id=uuid4(),
                name="Synthetic new part", supplier_id=self.skp_id, supplier_sku="new-part",
                sku_identity={"brand": "Topeak", "source_code": "000-NEW-PART", "code_kind": "manufacturer",
                    "source_ref": "Synthetic manufacturer packing label", "leaf_confirmed": True}))
        async with self.sessions() as db:
            self.assertIsNone(await db.scalar(select(StockBalance.id).where(
                StockBalance.product_id == new_card["results"][0]["product_id"])))
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)), 1)

        # SKP's PDF conversion intentionally has no fabricated EAN. Its next
        # invoice must still match that same product by the saved supplier SKU.
        invoice = Invoice("9/26/99999", date(2026, 1, 1), date(2026, 1, 15),
            D("22"), D("5.06"), D("27.06"), (
                InvoiceLine(1, "shared-part", "Synthetic shared part", D(1), D(14), D(23), D(14), D("17.22")),
                InvoiceLine(2, "new-part", "Synthetic new part", D(1), D(8), D(23), D(8), D("9.84")),
            ))
        prepared, second = await self.receive_csv(invoice.key, invoice.csv_bytes())
        self.assertEqual([line["ean"] for line in prepared["lines"]], ["", ""])
        self.assertEqual([line["product_code"] for line in prepared["lines"]],
                         ["PL-existing-part", "TP-000-NEW-PART"])
        self.assertEqual((second["products_created"], second["movements_created"]), (0, 2))

        async with self.sessions() as db:
            product = await db.get(Product, self.product_id)
            self.assertEqual((product.sku, product.supplier_id), ("PL-existing-part", self.supplier_id))
            self.assertIsNone(await db.scalar(select(Product.id).where(Product.sku == "SKP-shared-part")))
            new_product = await db.scalar(select(Product).where(Product.sku == "TP-000-NEW-PART"))
            self.assertEqual(new_product.id, new_card["results"][0]["product_id"])
            self.assertIsNone(await db.scalar(select(Product.id).where(Product.sku == "SKP-new-part")))
            self.assertEqual(await db.scalar(select(ProductIdentifier.product_id).where(
                ProductIdentifier.value == "shared-part",
                ProductIdentifier.supplier_id == self.skp_id,
                ProductIdentifier.identifier_type == IdentifierType.supplier_sku)), self.product_id)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductSupplySource)), 2)
            balance = await db.scalar(select(StockBalance).where(StockBalance.product_id == self.product_id))
            self.assertEqual((balance.qty_on_hand, balance.total_value), (D(3), D(34)))
            new_balance = await db.scalar(select(StockBalance).where(StockBalance.product_id == new_product.id))
            self.assertEqual((new_balance.qty_on_hand, new_balance.total_value), (D(1), D(8)))
            original_movements = list((await db.execute(select(StockMovement.id, StockMovement.quantity,
                StockMovement.unit_cost, StockMovement.total_cost, StockMovement.balance_after,
                StockMovement.reference_source).order_by(StockMovement.id))).all())
            self.assertEqual(len(original_movements), 3)

        async with self.sessions() as db:
            recount = await stock_adjustments.preview(db, StockAdjustmentPreview(
                request_id=uuid4(), sku="PL-existing-part", warehouse_code="receipt-test",
                counted_quantity="1", counted_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                source_reference="SKP isolated test count", operator_name="Test operator",
                reason="Synthetic physical recount"))
        apply_request = StockAdjustmentApply(preview_hash=recount["preview_hash"],
            confirmed=True, quantities_verified=True, costs_documented=True)
        async with self.sessions() as db:
            result = await stock_adjustments.apply(db, recount["id"], apply_request)
            await db.commit()
        async with self.sessions() as db:
            self.assertEqual(await stock_adjustments.apply(db, recount["id"], apply_request), result)
            await db.commit()
        self.assertEqual(result["delta"], "-2")

        async with self.sessions() as db:
            # Recount consumes the earlier 2 x 10 layer, leaving 1 x 14. It must
            # append a movement rather than rewriting either receipt's cost.
            balance = await db.scalar(select(StockBalance).where(StockBalance.product_id == self.product_id))
            self.assertEqual((balance.qty_on_hand, balance.total_value, balance.avg_cost), (D(1), D(14), D(14)))
            movement = await db.get(StockMovement, result["movement_id"])
            self.assertEqual((movement.movement_type, movement.total_cost), (MovementType.ADJUSTMENT_OUT, D(20)))
            layers = (await db.scalars(select(FifoLayer).where(FifoLayer.product_id == self.product_id)
                                      .order_by(FifoLayer.id))).all()
            self.assertEqual([(layer.quantity_original, layer.quantity_remaining, layer.unit_cost)
                              for layer in layers], [(D(2), D(0), D(10)), (D(1), D(1), D(14))])
            allocation = await db.scalar(select(FifoAllocation).where(FifoAllocation.issue_movement_id == movement.id))
            self.assertEqual((allocation.layer_id, allocation.quantity, allocation.unit_cost_at_issue),
                             (layers[0].id, D(2), D(10)))
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)), 4)
            after = list((await db.execute(select(StockMovement.id, StockMovement.quantity,
                StockMovement.unit_cost, StockMovement.total_cost, StockMovement.balance_after,
                StockMovement.reference_source).where(StockMovement.reference_type == "receiving_session")
                .order_by(StockMovement.id))).all())
            self.assertEqual(after, original_movements)
