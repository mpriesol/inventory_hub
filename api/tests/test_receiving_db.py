"""Receiving ledger regressions in a guarded, disposable localhost PostgreSQL schema."""
import asyncio
import os
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.db_models import Product, Supplier, Warehouse, ReceivingStatus
from inventory_hub.db_models_ext import ReceivingLine, ReceivingSession, StockBalance, StockMovement
from inventory_hub.routers import receiving_db as receiving
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.services.stock_balances import lock_stock_balances

TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")
D = Decimal


class ReceivingInputTests(unittest.TestCase):
    def test_invalid_quantities_are_rejected_before_database_writes(self):
        for value in ("0", "-1", "NaN", "Infinity", "0.0001"):
            with self.subTest(scan=value), self.assertRaises(ValidationError):
                receiving.ScanRequest(code="EAN", qty=value)
        for value in ("-1", "NaN", "Infinity", "0.0001"):
            with self.subTest(set_qty=value), self.assertRaises(ValidationError):
                receiving.SetQtyRequest(line_index=0, received_qty=value)
        self.assertEqual(receiving.SetQtyRequest(line_index=0, received_qty="0").received_qty, D("0"))


@unittest.skipUnless(TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class ReceivingDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("Receiving tests require a dedicated localhost *_catalog_test database")
        self.schema = "receiving_test_" + uuid4().hex
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            root = Path(__file__).resolve().parents[2] / "infra" / "db-init"
            await connection.execute((root / "001_schema.sql").read_text())
            # 002 checks enum names across all schemas, so create this schema's
            # enum explicitly when other isolated test schemas already exist.
            await connection.execute("CREATE TYPE payment_status AS ENUM ('unpaid', 'partial', 'paid')")
            await connection.execute((root / "002_invoice_management.sql").read_text())
            await connection.execute((root / "007_order_stock.sql").read_text())
            await connection.execute((root / "010_stock_publication.sql").read_text())
            await connection.execute((root / "011_fifo.sql").read_text())
        finally:
            await connection.close()
        self.engine = create_async_engine(
            TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1),
            poolclass=NullPool, connect_args={"server_settings": {"search_path": self.schema}},
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        self.index_patch = patch.object(receiving, "_update_invoice_status", return_value=True)
        self.index = self.index_patch.start()
        self.prefix_patch = patch.object(receiving, "_product_code_prefix", return_value="TEST-")
        self.prefix_patch.start()
        async with self.sessions() as db:
            supplier = Supplier(code="receipt-test", name="Test receiving supplier")
            warehouse = Warehouse(code="receipt-test", name="Test warehouse")
            product = Product(sku="RECEIPT-A", name="Test product")
            db.add_all([supplier, warehouse, product])
            await db.commit()
            self.supplier_id, self.warehouse_id, self.product_id = supplier.id, warehouse.id, product.id

    async def asyncTearDown(self):
        self.prefix_patch.stop()
        self.index_patch.stop()
        await self.engine.dispose()
        connection = await asyncpg.connect(TEST_URL)
        try:
            # Only the random schema created by this test, in the guarded DB.
            await connection.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            await connection.close()

    async def make_session(self, rows=None, supplier_id=None):
        if rows is None:
            rows = [{"product_id": self.product_id, "received_qty": D("2"), "unit_price": D("5")}]
        async with self.sessions() as db:
            receipt = ReceivingSession(
                supplier_id=supplier_id or self.supplier_id, warehouse_id=self.warehouse_id,
                invoice_number="TEST-" + uuid4().hex, status=ReceivingStatus.in_progress,
                total_lines=len(rows), session_data={"keep": "existing metadata"},
            )
            db.add(receipt)
            await db.flush()
            for i, row in enumerate(rows, 1):
                values = {"ordered_qty": D("2"), "received_qty": D("0"), **row}
                db.add(ReceivingLine(
                    session_id=receipt.id, line_number=i,
                    status=receiving._status_for(values["received_qty"], values["ordered_qty"]), **values,
                ))
            await db.commit()
            return receipt.id

    async def finalize(self, session_id, supplier_code="receipt-test", force=False):
        async with self.sessions() as db:
            try:
                return await receiving.finalize_session(supplier_code, session_id, receiving.FinalizeRequest(force=force), db)
            except Exception:
                await db.rollback()
                raise

    async def counts(self, session_id):
        async with self.sessions() as db:
            receipt = await db.get(ReceivingSession, session_id)
            count = (await db.execute(select(func.count()).where(
                StockMovement.reference_type == "receiving_session", StockMovement.reference_id == str(session_id)
            ))).scalar_one()
            return receipt.status, count

    async def test_retry_returns_exact_committed_result_and_repairs_index(self):
        session_id = await self.make_session()
        self.index.side_effect = OSError("test index unavailable")
        with self.assertLogs(receiving.logger, level="WARNING"):
            result = await self.finalize(session_id)
        self.index.side_effect = None
        replay = await self.finalize(session_id)
        self.assertEqual(replay, result)
        self.assertEqual(await self.counts(session_id), (ReceivingStatus.completed, 1))
        self.assertEqual(self.index.call_count, 2)
        async with self.sessions() as db:
            receipt = await db.get(ReceivingSession, session_id)
            self.assertEqual(receipt.session_data["keep"], "existing metadata")

    async def test_shared_balance_lock_distinguishes_existing_zero_from_new_and_rolled_back_rows(self):
        async with self.sessions() as db:
            product = Product(sku="RECEIPT-NEW-BALANCE", name="Product without stock balance")
            db.add(product)
            db.add(StockBalance(product_id=self.product_id, warehouse_id=self.warehouse_id))
            await db.commit()
            product_ids = {self.product_id, product.id}
            new_product_id = product.id

        async with self.sessions() as db:
            balances, created = await lock_stock_balances(db, product_ids, self.warehouse_id)
            self.assertEqual(set(balances), product_ids)
            self.assertEqual(created, {new_product_id})
            self.assertEqual(balances[self.product_id].qty_on_hand, D("0"))
            await db.rollback()

        async with self.sessions() as db:
            _, created = await lock_stock_balances(db, product_ids, self.warehouse_id)
            self.assertEqual(created, {new_product_id})
            await db.commit()

        async with self.sessions() as db:
            _, created = await lock_stock_balances(db, product_ids, self.warehouse_id)
            self.assertEqual(created, set())

    async def run_held_receipts(self, first_id, second_id):
        entered, release = asyncio.Event(), asyncio.Event()
        original = receiving._write_stock_for_line

        async def held(db, session, *args, **kwargs):
            result = await original(db, session, *args, **kwargs)
            if session.id == first_id and not entered.is_set():
                entered.set()
                await asyncio.wait_for(release.wait(), 5)
            return result

        with patch.object(receiving, "_write_stock_for_line", held):
            first = asyncio.create_task(self.finalize(first_id))
            await asyncio.wait_for(entered.wait(), 5)
            second = asyncio.create_task(self.finalize(second_id))
            try:
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(second), 0.1)
            finally:
                release.set()
            return await asyncio.wait_for(asyncio.gather(first, second), 5)

    async def test_simultaneous_finalize_receives_once(self):
        session_id = await self.make_session()
        first, second = await self.run_held_receipts(session_id, session_id)
        self.assertEqual(first, second)
        self.assertEqual(await self.counts(session_id), (ReceivingStatus.completed, 1))

    async def test_concurrent_receipts_create_one_balance_without_lost_stock(self):
        first_id = await self.make_session()
        second_id = await self.make_session([
            {"product_id": self.product_id, "received_qty": D("3"), "unit_price": D("10")}
        ])
        await self.run_held_receipts(first_id, second_id)
        async with self.sessions() as db:
            balance = (await db.execute(select(StockBalance).where(
                StockBalance.product_id == self.product_id, StockBalance.warehouse_id == self.warehouse_id,
            ))).scalar_one()
            self.assertEqual(balance.qty_on_hand, D("5"))
            self.assertEqual(balance.avg_cost, D("8"))
            self.assertEqual(balance.total_value, D("40"))

    async def test_existing_balance_concurrent_receipts_preserve_quantity_and_value(self):
        async with self.sessions() as db:
            db.add(StockBalance(product_id=self.product_id, warehouse_id=self.warehouse_id,
                                qty_on_hand=D("2"), avg_cost=D("5"), total_value=D("10")))
            await db.commit()
        first_id, second_id = await self.make_session(), await self.make_session()
        await self.run_held_receipts(first_id, second_id)
        async with self.sessions() as db:
            balance = (await db.execute(select(StockBalance).where(
                StockBalance.product_id == self.product_id, StockBalance.warehouse_id == self.warehouse_id,
            ))).scalar_one()
            self.assertEqual((balance.qty_on_hand, balance.avg_cost, balance.total_value), (D("6"), D("5"), D("30")))

    async def test_reverse_product_order_receipts_complete_without_deadlock(self):
        async with self.sessions() as db:
            product = Product(sku="RECEIPT-B", name="Test second product")
            db.add(product)
            await db.commit()
            second_product_id = product.id
        rows = [{"product_id": product_id, "received_qty": D("2"), "unit_price": D("5")}
                for product_id in (self.product_id, second_product_id)]
        first_id = await self.make_session(rows)
        second_id = await self.make_session(list(reversed(rows)))
        await asyncio.wait_for(asyncio.gather(self.finalize(first_id), self.finalize(second_id)), 5)
        async with self.sessions() as db:
            balances = (await db.execute(select(StockBalance).where(
                StockBalance.warehouse_id == self.warehouse_id,
            ))).scalars().all()
            self.assertEqual(len(balances), 2)
            self.assertTrue(all(balance.qty_on_hand == D("4") for balance in balances))

    async def test_simultaneous_scans_keep_both_increments(self):
        session_id = await self.make_session([
            {"product_id": self.product_id, "supplier_sku": "scan-item", "unit_price": D("5")}
        ])
        entered, release = asyncio.Event(), asyncio.Event()
        original = receiving._count_unexpected

        async def held(db, receipt_id):
            result = await original(db, receipt_id)
            if not entered.is_set():
                entered.set()
                await asyncio.wait_for(release.wait(), 5)
            return result

        async def scan():
            async with self.sessions() as db:
                result = await receiving.scan_code("receipt-test", session_id, receiving.ScanRequest(code="scan-item"), db)
                await db.commit()
                return result

        with patch.object(receiving, "_count_unexpected", held):
            first = asyncio.create_task(scan())
            await asyncio.wait_for(entered.wait(), 5)
            second = asyncio.create_task(scan())
            try:
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(second), 0.1)
            finally:
                release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 5)
        async with self.sessions() as db:
            line = (await db.execute(select(ReceivingLine).where(ReceivingLine.session_id == session_id))).scalar_one()
            self.assertEqual(line.received_qty, D("2"))

    async def test_unknown_cost_or_identity_blocks_entire_receipt_even_forced(self):
        for invalid in ({"product_id": self.product_id, "unit_price": None}, {"unit_price": D("5")}):
            with self.subTest(invalid=invalid):
                session_id = await self.make_session([
                    {"product_id": self.product_id, "received_qty": D("2"), "unit_price": D("5")},
                    {"received_qty": D("2"), **invalid},
                ])
                with self.assertRaises(HTTPException) as error:
                    await self.finalize(session_id, force=True)
                self.assertEqual(error.exception.status_code, 409)
                self.assertEqual(await self.counts(session_id), (ReceivingStatus.in_progress, 0))
        self.index.assert_not_called()

    async def test_zero_quantity_does_not_create_product_balance_or_movement(self):
        session_id = await self.make_session([{"received_qty": D("0"), "unit_price": None}])
        result = await self.finalize(session_id, force=True)
        self.assertFalse(result["stock_movements_created"])
        self.assertEqual(result["products_created"], 0)
        self.assertEqual(await self.counts(session_id), (ReceivingStatus.completed, 0))
        async with self.sessions() as db:
            count = (await db.execute(select(func.count()).where(StockBalance.warehouse_id == self.warehouse_id))).scalar_one()
            self.assertEqual(count, 0)

    async def test_error_after_ledger_append_rolls_back_without_index_update(self):
        session_id = await self.make_session()
        original = receiving._write_stock_for_line

        async def fail_after_write(*args, **kwargs):
            await original(*args, **kwargs)
            raise RuntimeError("test failure before commit")

        with patch.object(receiving, "_write_stock_for_line", fail_after_write), self.assertRaises(RuntimeError):
            await self.finalize(session_id)
        self.assertEqual(await self.counts(session_id), (ReceivingStatus.in_progress, 0))
        self.index.assert_not_called()
        result = await self.finalize(session_id)
        self.assertEqual(result["movements_created"], 1)

    async def test_cross_supplier_concurrent_ean_conflict_cannot_receive_wrong_identity(self):
        async with self.sessions() as db:
            supplier = Supplier(code="receipt-other", name="Other test supplier")
            db.add(supplier)
            await db.commit()
            supplier_id = supplier.id
        ean = "4006381333931"
        first_id = await self.make_session([{"supplier_sku": "new-a", "ean": ean, "received_qty": D("2"), "unit_price": D("5")}])
        second_id = await self.make_session([{"supplier_sku": "new-b", "ean": ean, "received_qty": D("2"), "unit_price": D("5")}], supplier_id)
        original = ProductIdentifierService.find_product_by_barcode
        both_unmatched, calls = asyncio.Event(), []

        async def concurrent_lookup(service, code):
            result = await original(service, code)
            if code == ean and result is None:
                calls.append(service)
                if len(calls) == 2:
                    both_unmatched.set()
                await asyncio.wait_for(both_unmatched.wait(), 5)
            return result

        with patch.object(ProductIdentifierService, "find_product_by_barcode", concurrent_lookup):
            results = await asyncio.wait_for(asyncio.gather(
                self.finalize(first_id), self.finalize(second_id, "receipt-other"), return_exceptions=True,
            ), 10)
        errors = [result for result in results if isinstance(result, Exception)]
        self.assertEqual(len(errors), 1, results)
        self.assertIsInstance(errors[0], HTTPException)
        self.assertEqual(errors[0].status_code, 409)
        async with self.sessions() as db:
            products = (await db.execute(select(Product).where(Product.sku.in_(("TEST-new-a", "TEST-new-b"))))).scalars().all()
            self.assertEqual(len(products), 1)
            count = (await db.execute(select(func.count()).where(StockMovement.reference_id.in_((str(first_id), str(second_id))),
                                                               StockMovement.reference_type == "receiving_session"))).scalar_one()
            self.assertEqual(count, 1)
