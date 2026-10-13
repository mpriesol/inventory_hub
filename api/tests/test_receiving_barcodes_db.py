"""Read-only invoice barcode enrichment and partial receipt ledger regressions."""
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import func, select, text

import test_receiving_db as fixture
from inventory_hub.db_models import IdentifierType, Product, ProductIdentifier, ReceivingStatus, Supplier, SupplierProduct
from inventory_hub.db_models_ext import ReceivingLine, ReceivingSession, StockBalance, StockMovement
from inventory_hub.routers import receiving_db as receiving
from inventory_hub.services.identifiers import ProductIdentifierService


@unittest.skipUnless(fixture.TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class ReceivingBarcodesDatabaseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ReceivingDatabaseTests.asyncSetUp
    asyncTearDown = fixture.ReceivingDatabaseTests.asyncTearDown
    make_session = fixture.ReceivingDatabaseTests.make_session
    finalize = fixture.ReceivingDatabaseTests.finalize
    counts = fixture.ReceivingDatabaseTests.counts

    async def test_summary_sources_are_supplier_scoped_and_do_not_change_invoice_or_identity(self):
        async with self.sessions() as db:
            other = Supplier(code="barcode-other", name="Other supplier")
            db.add(other)
            await db.flush()
            db.add_all([
                SupplierProduct(supplier_id=self.supplier_id, supplier_sku="catalog-only", name="Feed item", ean="5901234123457"),
                SupplierProduct(supplier_id=self.supplier_id, supplier_sku="known", name="Stale feed barcode", ean="5901234123457"),
                SupplierProduct(supplier_id=other.id, supplier_sku="catalog-only", name="Other item", ean="9780201379624"),
                SupplierProduct(supplier_id=other.id, supplier_sku="other-only", name="Other item", ean="9780201379624"),
                SupplierProduct(supplier_id=self.supplier_id, supplier_sku="invalid", name="Invalid barcode", ean="0000000000000"),
                SupplierProduct(supplier_id=self.supplier_id, supplier_sku="inactive", name="Inactive item", ean="9780201379624", is_active=False),
            ])
            identifiers = ProductIdentifierService(db)
            await identifiers.add_identifier(self.product_id, "4006381333931")
            await identifiers.add_identifier(self.product_id, "known", IdentifierType.supplier_sku, supplier_id=self.supplier_id)
            await db.commit()
        session_id = await self.make_session([
            {"ean": "012345678905", "supplier_sku": "invoice"},
            {"supplier_sku": "known"},
            {"supplier_sku": "catalog-only"},
            {"supplier_sku": "other-only"},
            {"supplier_sku": "invalid"},
            {"supplier_sku": "inactive"},
        ])
        async with self.sessions() as db:
            before = list((await db.execute(text("SELECT id, ean, product_id, line_fingerprint FROM receiving_lines WHERE session_id = :session ORDER BY id"), {"session": session_id})).all())
            identifier_count = await db.scalar(select(func.count()).select_from(ProductIdentifier))
            summary = await receiving.get_session_summary("receipt-test", session_id, db)
            self.assertEqual([(line["display_ean"], line["ean_source"]) for line in summary["lines"]], [
                ("012345678905", "invoice"), ("4006381333931", "product"),
                ("5901234123457", "supplier_catalog"), ("", None), ("", None), ("", None),
            ])
            self.assertEqual([line["ean"] for line in summary["lines"]], ["012345678905", "", "", "", "", ""])
            await db.commit()
            after = list((await db.execute(text("SELECT id, ean, product_id, line_fingerprint FROM receiving_lines WHERE session_id = :session ORDER BY id"), {"session": session_id})).all())
            self.assertEqual(after, before)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductIdentifier)), identifier_count)
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)), 0)

    async def test_conflicts_do_not_supply_another_products_barcode(self):
        async with self.sessions() as db:
            other = Product(sku="TEST-other", name="Different physical product")
            db.add(other)
            await db.flush()
            ids = ProductIdentifierService(db)
            await ids.add_identifier(other.id, "5901234123457")
            await ids.add_identifier(other.id, "conflicting-alias", IdentifierType.supplier_sku, supplier_id=self.supplier_id)
            db.add_all([
                SupplierProduct(supplier_id=self.supplier_id, supplier_sku="feed-conflict", name="Conflicting feed", ean="5901234123457"),
                SupplierProduct(supplier_id=self.supplier_id, supplier_sku="conflicting-alias", name="Conflicting alias", ean="5901234123457"),
            ])
            await db.commit()
        session_id = await self.make_session([
            {"product_id": self.product_id, "supplier_sku": "feed-conflict"},
            {"product_id": self.product_id, "supplier_sku": "conflicting-alias"},
        ])
        async with self.sessions() as db:
            summary = await receiving.get_session_summary("receipt-test", session_id, db)
            self.assertEqual([(line["display_ean"], line["ean_source"]) for line in summary["lines"]], [("", None), ("", None)])
            self.assertEqual([line["product_id"] for line in summary["lines"]], [self.product_id, self.product_id])

    async def test_create_and_manual_edit_keep_catalog_ean_out_of_invoice_source(self):
        async with self.sessions() as db:
            db.add(SupplierProduct(supplier_id=self.supplier_id, supplier_sku="feed-only", name="Feed item", ean="5901234123457"))
            await db.commit()
        path = Path(self.config_folder.name) / "display.csv"
        path.write_text("SCM;TITLE;QTY;PRICE;EAN\nfeed-only;Synthetic item;3;10;\n")
        with patch.object(receiving, "_invoice_csv_path", return_value=path):
            async with self.sessions() as db:
                prepared = await receiving.create_session("receipt-test", receiving.CreateSessionRequest(
                    invoice_id="receipt-test:display", warehouse_code="receipt-test"), db)
                await db.commit()
        line = prepared["lines"][0]
        self.assertEqual((line["ean"], line["display_ean"], line["ean_source"], line["product_id"]),
                         ("", "5901234123457", "supplier_catalog", None))
        async with self.sessions() as db:
            edited = await receiving.set_line_quantity("receipt-test", prepared["session_id"],
                receiving.SetQtyRequest(line_index=0, received_qty="1"), db)
            self.assertEqual((edited["line"]["received_qty"], edited["line"]["display_ean"]), (1, "5901234123457"))
            await db.commit()
            stored = await db.get(ReceivingLine, line["id"])
            self.assertEqual(stored.ean or "", "")
            self.assertIsNone(stored.product_id)
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)), 0)

    async def test_partial_historical_receipt_only_posts_confirmed_quantity_and_replays(self):
        session_id = await self.make_session([
            {"product_id": self.product_id, "ordered_qty": D(5), "received_qty": D(2), "unit_price": D(7)},
            {"supplier_sku": "already-sold", "ordered_qty": D(4), "received_qty": D(0), "unit_price": None},
        ])
        result = await self.finalize(session_id, force=True)
        self.assertEqual(result, await self.finalize(session_id, force=True))
        self.assertEqual((result["total_received"], result["total_ordered"], result["movements_created"], result["products_created"]), (2, 9, 1, 0))
        self.assertEqual((result["stats"]["received_partial"], result["stats"]["not_received"]), (1, 1))
        self.assertEqual(await self.counts(session_id), (ReceivingStatus.completed, 1))
        async with self.sessions() as db:
            balance = await db.scalar(select(StockBalance).where(StockBalance.product_id == self.product_id))
            self.assertEqual((balance.qty_on_hand, balance.total_value), (D(2), D(14)))
            self.assertIsNone(await db.scalar(select(Product.id).where(Product.sku == "TEST-already-sold")))
            receipt = await db.get(ReceivingSession, session_id)
            self.assertEqual(receipt.session_data["finalize_result"], result)

    async def test_empty_historical_receipt_completion_replays_without_creating_stock(self):
        session_id = await self.make_session([{"supplier_sku": "already-sold", "received_qty": D(0), "unit_price": None}])
        result = await self.finalize(session_id, force=True)
        self.assertEqual(result, await self.finalize(session_id, force=True))
        self.assertEqual(await self.counts(session_id), (ReceivingStatus.completed, 0))
        self.assertEqual((result["movements_created"], result["products_created"]), (0, 0))
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockBalance)), 0)
            self.assertIsNone(await db.scalar(select(Product.id).where(Product.sku == "TEST-already-sold")))
