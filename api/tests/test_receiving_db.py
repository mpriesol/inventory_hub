"""Receiving ledger regressions in a guarded, disposable localhost PostgreSQL schema."""
import asyncio
import os
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.db_models import Product, Supplier, Warehouse, ReceivingStatus, ProductIdentifier, IdentifierType
from inventory_hub.db_models_ext import ReceivingLine, ReceivingSession, StockBalance, StockMovement, ScanEvent
from inventory_hub.routers import receiving_db as receiving
from inventory_hub.services.identifiers import ProductIdentifierService, IdentifierConflict
from inventory_hub.receiving_scan_models import ReceivingScanRequest
from inventory_hub.services.stock_balances import lock_stock_balances

TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")
D = Decimal


class ReceivingInputTests(unittest.TestCase):
    def test_scan_operation_id_code_and_line_selection_validation(self):
        for data in ({"code": ""}, {"code": "   "}, {"code": "A" * 101},
                     {"code": "EAN", "request_id": "not-a-uuid"},
                     {"code": "EAN", "line_id": 0}, {"code": "EAN", "scanned_by": ""}):
            with self.subTest(data=data), self.assertRaises(ValidationError):
                receiving.ScanRequest(**data)
        request_id = uuid4()
        self.assertEqual(receiving.ScanRequest(code="EAN", request_id=str(request_id)).request_id, request_id)
        self.assertIsNone(receiving.ScanRequest(code="EAN").request_id)

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
            await connection.execute((root / "013_receiving_scan_requests.sql").read_text())
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

    async def test_cross_supplier_concurrent_receipts_share_one_barcode_identity(self):
        async with self.sessions() as db:
            supplier = Supplier(code="receipt-other", name="Other test supplier")
            db.add(supplier)
            await db.commit()
            supplier_id = supplier.id
        ean = "4006381333931"
        first_id = await self.make_session([{"supplier_sku": "new-a", "ean": ean, "received_qty": D("2"), "unit_price": D("5")}])
        second_id = await self.make_session([{"supplier_sku": "new-b", "ean": ean, "received_qty": D("2"), "unit_price": D("5")}], supplier_id)
        # Different supplier aliases for the same unique verified barcode reuse
        # one physical product, as shop pulls and catalog imports already do.
        results = await asyncio.wait_for(asyncio.gather(
            self.finalize(first_id), self.finalize(second_id, "receipt-other")), 10)
        self.assertEqual(sum(result["products_created"] for result in results), 1)
        async with self.sessions() as db:
            products = (await db.execute(select(Product).where(Product.sku.in_(("TEST-new-a", "TEST-new-b"))))).scalars().all()
            self.assertEqual(len(products), 1)
            balance = await db.scalar(select(StockBalance).where(StockBalance.product_id == products[0].id))
            self.assertEqual(balance.qty_on_hand, D("4"))
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductIdentifier).where(
                ProductIdentifier.product_id == products[0].id,
                ProductIdentifier.identifier_type == IdentifierType.supplier_sku)), 2)

    async def scan(self, session_id, request):
        async with self.sessions() as db:
            try:
                result = await receiving.scan_code("receipt-test", session_id, request, db)
                await db.commit()
                return result
            except Exception:
                await db.rollback()
                raise

    async def test_ten_physical_scans_and_retries_add_exactly_ten_then_finalize_once(self):
        ean = "4006381333931"
        session_id = await self.make_session([{"product_id": self.product_id, "ean": ean,
            "ordered_qty": D("10"), "unit_price": D("5")}])
        requests = [receiving.ScanRequest(code=ean, request_id=uuid4()) for _ in range(10)]
        for position, request in enumerate(requests, 1):
            result = await self.scan(session_id, request)
            self.assertEqual(result["line"]["received_qty"], position)
            self.assertFalse(result["replayed"])
            self.assertEqual(result["line"]["product_code"], "RECEIPT-A")
        first_retry = await self.scan(session_id, requests[0])
        self.assertEqual(first_retry["line"]["received_qty"], 1, "Replay returns the original committed result")
        self.assertTrue(first_retry["replayed"])
        result = await self.finalize(session_id)
        replay = await self.scan(session_id, requests[-1])
        self.assertTrue(replay["replayed"], "A successful old scan is recoverable after finalize")
        self.assertEqual(result, await self.finalize(session_id))
        async with self.sessions() as db:
            line = await db.scalar(select(ReceivingLine).where(ReceivingLine.session_id == session_id))
            self.assertEqual(line.received_qty, D("10"))
            self.assertEqual(await db.scalar(select(func.count()).select_from(ScanEvent).where(
                ScanEvent.receiving_session_id == session_id)), 10)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ReceivingScanRequest).where(
                ReceivingScanRequest.session_id == session_id)), 10)
            balance = await db.scalar(select(StockBalance).where(StockBalance.product_id == self.product_id))
            self.assertEqual(balance.qty_on_hand, D("10"))

    async def test_simultaneous_same_scan_uuid_adds_once_and_payload_reuse_conflicts(self):
        session_id = await self.make_session([{"product_id": self.product_id, "supplier_sku": "scan-item", "unit_price": D("5")}])
        request = receiving.ScanRequest(code="scan-item", request_id=uuid4())
        results = await asyncio.wait_for(asyncio.gather(self.scan(session_id, request), self.scan(session_id, request)), 5)
        self.assertEqual(sorted(result["replayed"] for result in results), [False, True])
        self.assertTrue(all(result["line"]["received_qty"] == 1 for result in results))
        for changed in ({"qty": "2"}, {"code": "other"}, {"scanned_by": "other"}):
            with self.subTest(changed=changed), self.assertRaises(HTTPException) as error:
                await self.scan(session_id, receiving.ScanRequest(**{**request.model_dump(), **changed}))
            self.assertEqual(error.exception.status_code, 409)
            self.assertEqual(error.exception.detail["code"], "scan_request_conflict")
        second_id = await self.make_session([{"product_id": self.product_id, "supplier_sku": "scan-item", "unit_price": D("5")}])
        with self.assertRaises(HTTPException) as error:
            await self.scan(second_id, request)
        self.assertEqual(error.exception.detail["code"], "scan_request_conflict")

    async def test_scan_retry_after_quantity_reset_does_not_reapply_old_operation(self):
        session_id = await self.make_session([{"product_id": self.product_id, "supplier_sku": "scan-item", "unit_price": D("5")}])
        request = receiving.ScanRequest(code="scan-item", request_id=uuid4())
        await self.scan(session_id, request)
        async with self.sessions() as db:
            await receiving.reset_all_items("receipt-test", session_id, db)
            await db.commit()
        self.assertTrue((await self.scan(session_id, request))["replayed"])
        async with self.sessions() as db:
            line = await db.scalar(select(ReceivingLine).where(ReceivingLine.session_id == session_id))
            self.assertEqual(line.received_qty, D("0"))

    async def test_unexpected_scan_is_idempotent_too(self):
        session_id = await self.make_session()
        request = receiving.ScanRequest(code="UNKNOWN-CODE", request_id=uuid4())
        result = await self.scan(session_id, request)
        replay = await self.scan(session_id, request)
        self.assertEqual(result["status"], "unexpected")
        self.assertEqual(result["summary"]["unexpected"], 1)
        self.assertEqual(replay, {**result, "replayed": True})

    async def test_ambiguous_invoice_rows_need_explicit_line_and_keep_costs_separate(self):
        ean = "4006381333931"
        session_id = await self.make_session([
            {"product_id": self.product_id, "ean": ean, "ordered_qty": D("1"), "unit_price": D("80")},
            {"product_id": self.product_id, "ean": ean, "ordered_qty": D("1"), "unit_price": D("90")},
        ])
        with self.assertRaises(HTTPException) as error:
            await self.scan(session_id, receiving.ScanRequest(code=ean, request_id=uuid4()))
        self.assertEqual(error.exception.detail["code"], "scan_line_ambiguous")
        for line_id in error.exception.detail["line_ids"]:
            await self.scan(session_id, receiving.ScanRequest(code=ean, request_id=uuid4(), line_id=line_id))
        with self.assertRaises(HTTPException) as error:
            await self.scan(session_id, receiving.ScanRequest(code="OTHER", request_id=uuid4(), line_id=line_id))
        self.assertEqual(error.exception.detail["code"], "scan_line_mismatch")
        await self.finalize(session_id)
        async with self.sessions() as db:
            movements = (await db.execute(select(StockMovement).where(
                StockMovement.reference_type == "receiving_session", StockMovement.reference_id == str(session_id)
            ).order_by(StockMovement.id))).scalars().all()
            self.assertEqual([(row.quantity, row.unit_cost) for row in movements], [(D("1"), D("80")), (D("1"), D("90"))])

    async def test_finalize_revalidates_preassigned_product_and_rolls_back_every_line(self):
        ean = "4006381333931"
        async with self.sessions() as db:
            other = Product(sku="TEST-collision", name="Other owner")
            db.add(other)
            await db.flush()
            await ProductIdentifierService(db).add_identifier(other.id, ean)
            await db.commit()
        for identity in ({"ean": ean}, {"supplier_sku": "collision"}):
            with self.subTest(identity=identity):
                session_id = await self.make_session([
                    {"product_id": self.product_id, "received_qty": D("2"), "unit_price": D("5")},
                    {"product_id": self.product_id, "received_qty": D("2"), "unit_price": D("5"), **identity},
                ])
                with self.assertRaises(HTTPException) as error:
                    await self.finalize(session_id)
                self.assertEqual(error.exception.detail["code"], "receiving_identity_conflict")
                self.assertEqual(await self.counts(session_id), (ReceivingStatus.in_progress, 0))

    async def test_compound_ean_receipt_matches_each_barcode_and_creates_individual_identifiers(self):
        eans = ("5901234123457", "4006381333931")
        session_id = await self.make_session([{"supplier_sku": "compound", "ean": "/".join(eans), "unit_price": D("5")}])
        for ean in eans:
            await self.scan(session_id, receiving.ScanRequest(code=ean, request_id=uuid4()))
        result = await self.finalize(session_id)
        self.assertEqual(result["products_created"], 1)
        async with self.sessions() as db:
            product = await db.scalar(select(Product).where(Product.sku == "TEST-compound"))
            values = await ProductIdentifierService(db).get_all_barcodes(product.id)
            self.assertEqual(set(values), set(eans))

    async def test_unverified_barcode_lookup_never_selects_first_owner(self):
        async with self.sessions() as db:
            other = Product(sku="SECOND-UNVERIFIED", name="Other product")
            db.add(other)
            await db.flush()
            identifiers = ProductIdentifierService(db)
            await identifiers.add_identifier(self.product_id, "12345")
            await identifiers.add_identifier(other.id, "12345")
            with self.assertRaises(IdentifierConflict) as error:
                await identifiers.find_product_by_barcode("12345")
            self.assertEqual(error.exception.product_ids, sorted([self.product_id, other.id]))
            with self.assertRaises(ValueError):
                await identifiers.find_product_by_identifier("ANY", IdentifierType.supplier_sku)


    async def test_scan_migration_is_repeatable_preserves_receipt_and_widens_compound_ean(self):
        session_id = await self.make_session([{"product_id": self.product_id, "ean": "5901234123457/4006381333931",
            "ordered_qty": D("2"), "unit_price": D("5")}])
        async with self.sessions() as db:
            before = (await db.execute(text("SELECT line_fingerprint, ean FROM receiving_lines WHERE session_id=:id"),
                {"id": session_id})).one()
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'SET search_path TO "{self.schema}"')
            root = Path(__file__).resolve().parents[2] / "infra" / "db-init"
            await connection.execute((root / "013_receiving_scan_requests.sql").read_text())
            maximum = await connection.fetchval("SELECT character_maximum_length FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name='receiving_lines' AND column_name='ean'")
            self.assertEqual(maximum, 255)
        finally:
            await connection.close()
        async with self.sessions() as db:
            after = (await db.execute(text("SELECT line_fingerprint, ean FROM receiving_lines WHERE session_id=:id"),
                {"id": session_id})).one()
            self.assertEqual(after, before)


    async def test_same_uuid_in_concurrent_different_sessions_is_a_conflict_not_two_scans(self):
        rows = [{"product_id": self.product_id, "supplier_sku": "scan-item", "unit_price": D("5")}]
        first_id, second_id = await self.make_session(rows), await self.make_session(rows)
        request = receiving.ScanRequest(code="scan-item", request_id=uuid4())
        results = await asyncio.wait_for(asyncio.gather(
            self.scan(first_id, request), self.scan(second_id, request), return_exceptions=True), 5)
        errors = [result for result in results if isinstance(result, Exception)]
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], HTTPException)
        self.assertEqual(errors[0].detail["code"], "scan_request_conflict")
        async with self.sessions() as db:
            total = await db.scalar(select(func.sum(ReceivingLine.received_qty)).where(
                ReceivingLine.session_id.in_((first_id, second_id))))
            self.assertEqual(total, D("1"))

    async def test_failed_scan_rolls_back_quantity_and_original_uuid_can_be_retried(self):
        session_id = await self.make_session([{"product_id": self.product_id, "supplier_sku": "scan-item", "unit_price": D("5")}])
        request = receiving.ScanRequest(code="scan-item", request_id=uuid4())
        with patch.object(receiving, "_count_unexpected", AsyncMock(side_effect=RuntimeError("test before commit"))):
            with self.assertRaises(RuntimeError):
                await self.scan(session_id, request)
        result = await self.scan(session_id, request)
        self.assertEqual(result["line"]["received_qty"], 1)
        self.assertFalse(result["replayed"])
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(ScanEvent).where(
                ScanEvent.receiving_session_id == session_id)), 1)

    async def test_receipt_accepts_another_registered_ean_for_the_same_assigned_product(self):
        first_ean, second_ean = "5901234123457", "4006381333931"
        async with self.sessions() as db:
            identifiers = ProductIdentifierService(db)
            await identifiers.add_identifier(self.product_id, first_ean)
            await identifiers.add_identifier(self.product_id, second_ean)
            await db.commit()
        session_id = await self.make_session([{"product_id": self.product_id, "ean": first_ean, "unit_price": D("5")}])
        result = await self.scan(session_id, receiving.ScanRequest(code=second_ean, request_id=uuid4()))
        self.assertEqual(result["line"]["product_id"], self.product_id)
        self.assertEqual(result["line"]["received_qty"], 1)


    async def test_manual_quantity_preserves_long_compound_ean_without_overflowing_scan_event(self):
        ean = "5901234123457"
        raw_ean = "/".join([ean, "4006381333931"] * 5)
        self.assertGreater(len(raw_ean), 100)
        session_id = await self.make_session([{"product_id": self.product_id, "ean": raw_ean,
            "ordered_qty": D("2"), "unit_price": D("5")}])
        async with self.sessions() as db:
            result = await receiving.set_line_quantity("receipt-test", session_id,
                receiving.SetQtyRequest(line_index=0, received_qty="2"), db)
            await db.commit()
            self.assertEqual(result["line"]["ean"], raw_ean)
            event = await db.scalar(select(ScanEvent).where(ScanEvent.receiving_session_id == session_id))
            self.assertEqual((event.scanned_code, event.match_method, event.quantity), (ean, "manual", D("2")))
            line = await db.get(ReceivingLine, event.receiving_line_id)
            self.assertEqual(line.ean, raw_ean)
        result = await self.finalize(session_id)
        self.assertEqual(result["total_received"], 2)
        self.assertEqual(result["movements_created"], 1)

    async def test_manual_quantity_uses_real_supplier_or_canonical_code_without_truncating_source(self):
        source = "/".join(["not-a-barcode"] * 12)
        for supplier_sku, expected_code in (("REAL-SUPPLIER-CODE", "REAL-SUPPLIER-CODE"), (None, "RECEIPT-A")):
            with self.subTest(supplier_sku=supplier_sku):
                session_id = await self.make_session([{"product_id": self.product_id, "ean": source,
                    "supplier_sku": supplier_sku, "unit_price": D("5")}])
                async with self.sessions() as db:
                    await receiving.set_line_quantity("receipt-test", session_id,
                        receiving.SetQtyRequest(line_index=0, received_qty="1"), db)
                    await db.commit()
                    event = await db.scalar(select(ScanEvent).where(ScanEvent.receiving_session_id == session_id))
                    self.assertEqual(event.scanned_code, expected_code)
                    line = await db.get(ReceivingLine, event.receiving_line_id)
                    self.assertEqual(line.ean, source)
