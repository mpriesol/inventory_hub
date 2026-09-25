"""Opening inventory atomicity in an isolated localhost PostgreSQL schema only."""
import asyncio
import os
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.db_models import MovementType, Product, ReceivingStatus, Supplier, Warehouse
from inventory_hub.db_models_ext import ReceivingLine, ReceivingSession, StockBalance, StockMovement
from inventory_hub.opening_stock_models import OpeningStockBatch, OpeningStockLine
from inventory_hub.opening_stock_types import OpeningFinalizeRequest, OpeningPreviewRequest
from inventory_hub.routers import receiving_db as receiving
from inventory_hub.services import opening_stock as opening
from test_opening_stock import HEADER, NOW, preview_body


TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")
D = Decimal


@unittest.skipUnless(TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class OpeningStockDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("Opening tests require a dedicated localhost *_catalog_test database")
        self.schema = "opening_test_" + uuid4().hex
        self.sql_root = Path(__file__).resolve().parents[2] / "infra" / "db-init"
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            await connection.execute((self.sql_root / "001_schema.sql").read_text())
            await connection.execute((self.sql_root / "019_stock_tracking.sql").read_text())
            # The deployed 002 enum guard looks across schemas, so explicitly
            # create this schema's enum before invoking that historical file.
            await connection.execute("CREATE TYPE payment_status AS ENUM ('unpaid', 'partial', 'paid')")
            await connection.execute((self.sql_root / "002_invoice_management.sql").read_text())
            await connection.execute((self.sql_root / "006_opening_stock.sql").read_text())
            await connection.execute((self.sql_root / "007_order_stock.sql").read_text())
            await connection.execute((self.sql_root / "010_stock_publication.sql").read_text())
            await connection.execute((self.sql_root / "011_fifo.sql").read_text())
        finally:
            await connection.close()
        self.engine = create_async_engine(TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1),
            poolclass=NullPool, connect_args={"server_settings": {"search_path": self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        self.clock_patch = patch.object(opening, "now", return_value=NOW)
        self.clock = self.clock_patch.start()
        self.invoice_patch = patch.object(receiving, "_update_invoice_status", return_value=True)
        self.invoice_patch.start()
        self.prefix_patch = patch.object(receiving, "_product_code_prefix", return_value="TEST-")
        self.prefix_patch.start()
        async with self.sessions() as db:
            warehouse = Warehouse(code="opening-test", name="Opening test warehouse")
            supplier = Supplier(code="opening-receipt", name="Test receipt supplier")
            first = Product(sku="EXACT-SKU", name="First product")
            second = Product(sku="SECOND-SKU", name="Second product")
            db.add_all([warehouse, supplier, first, second])
            await db.commit()
            self.warehouse_id, self.supplier_id = warehouse.id, supplier.id
            self.first_id, self.second_id = first.id, second.id

    async def asyncTearDown(self):
        self.clock_patch.stop()
        self.invoice_patch.stop()
        self.prefix_patch.stop()
        await self.engine.dispose()
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            await connection.close()

    async def preview(self, **changes):
        async with self.sessions() as db:
            try:
                return await opening.preview(db, OpeningPreviewRequest(**preview_body(**changes)))
            except Exception:
                await db.rollback()
                raise

    async def batch(self, **changes):
        result = await self.preview(**changes)
        self.assertTrue(result["ready"], result)
        self.assertEqual(result["errors"], [])
        return result["batch"]

    async def finalize(self, batch, **changes):
        request = OpeningFinalizeRequest(preview_hash=changes.pop("preview_hash", batch["preview_hash"]),
                                         confirmed=True, receipts_reconciled=True, **changes)
        async with self.sessions() as db:
            try:
                return await opening.finalize(db, batch["id"], request)
            except Exception:
                await db.rollback()
                raise

    async def physical_snapshot(self):
        async with self.sessions() as db:
            return {table: (await db.execute(text(f"SELECT * FROM {table} ORDER BY id"))).mappings().all()
                    for table in ("products", "product_identifiers", "stock_balances", "stock_movements")}

    async def batch_count(self):
        async with self.sessions() as db:
            return await db.scalar(select(func.count()).select_from(OpeningStockBatch))

    async def assert_invalid_preview(self, **changes):
        try:
            result = await self.preview(**changes)
        except opening.OpeningError:
            return
        self.assertFalse(result["ready"])
        self.assertTrue(result["errors"])
        self.assertIsNone(result["batch"])

    async def make_receipt(self, quantity="3", cost="20"):
        async with self.sessions() as db:
            receipt = ReceivingSession(supplier_id=self.supplier_id, warehouse_id=self.warehouse_id,
                invoice_number="OPEN-RECEIPT-" + uuid4().hex, status=ReceivingStatus.in_progress,
                total_lines=1, session_data={})
            db.add(receipt)
            await db.flush()
            db.add(ReceivingLine(session_id=receipt.id, line_number=1, product_id=self.first_id,
                ordered_qty=D(quantity), received_qty=D(quantity), unit_price=D(cost), status="received"))
            await db.commit()
            return receipt.id

    async def receive(self, session_id):
        async with self.sessions() as db:
            try:
                return await receiving.finalize_session("opening-receipt", session_id, receiving.FinalizeRequest(), db)
            except Exception:
                await db.rollback()
                raise

    async def test_preview_freezes_audit_values_without_any_physical_or_product_writes(self):
        before = await self.physical_snapshot()
        request_id = str(uuid4())
        batch = await self.batch(request_id=request_id, counted_at=(NOW - timedelta(days=90)).isoformat(),
                                 csv_text=HEADER + "EXACT-SKU;2;10;ks\nSECOND-SKU;3;0;ks\n")
        self.assertEqual(batch["id"], request_id)
        self.assertEqual(batch["source_reference"], "Physical count sheet 42")
        self.assertEqual(batch["operator_name"], "Test operator")
        self.assertEqual((batch["unit"], batch["currency"], batch["price_basis"]), ("ks", "EUR", "ex_vat"))
        self.assertEqual((batch["summary"]["lines"], D(batch["summary"]["quantity"]), D(batch["summary"]["total_value"])),
                         (2, D("5"), D("20")))
        self.assertTrue(batch["warnings"])
        self.assertEqual(await self.physical_snapshot(), before)
        async with self.sessions() as db:
            persisted = await opening.get_batch(db, batch["id"])
        self.assertEqual(persisted["preview_hash"], batch["preview_hash"])
        self.assertEqual(persisted["counted_at"], batch["counted_at"])
        self.assertEqual(await self.batch_count(), 1)

    async def test_invalid_csv_unknown_or_case_only_sku_and_future_count_never_persist_batch(self):
        before = await self.physical_snapshot()
        for csv in (HEADER + "UNKNOWN-SKU;2;10;ks\n", HEADER + "exact-sku;2;10;ks\n",
                    HEADER + "EXACT-SKU;2;;ks\n", HEADER + "EXACT-SKU;2;10;\n",
                    HEADER + "EXACT-SKU;2;10;ks\nEXACT-SKU;3;20;ks\n"):
            await self.assert_invalid_preview(csv_text=csv)
        await self.assert_invalid_preview(counted_at=(NOW + timedelta(seconds=1)).isoformat())
        await self.assert_invalid_preview(warehouse_code="NO-SUCH-WAREHOUSE")
        self.assertEqual(await self.batch_count(), 0)
        self.assertEqual(await self.physical_snapshot(), before)

    async def test_preview_request_id_replay_is_idempotent_and_different_input_is_rejected(self):
        request_id = str(uuid4())
        first = await self.batch(request_id=request_id)
        replay = await self.batch(request_id=request_id)
        self.assertEqual(replay, first)
        await self.assert_invalid_preview(request_id=request_id, csv_text=HEADER + "EXACT-SKU;9;10;ks\n")
        self.assertEqual(await self.batch_count(), 1)

    async def test_case_colliding_canonical_sku_blocks_preview_and_new_collision_blocks_finalize(self):
        batch = await self.batch()
        async with self.sessions() as db:
            db.add(Product(sku="exact-sku", name="Conflicting case-only canonical code"))
            await db.commit()
        before = await self.physical_snapshot()
        response = await self.preview()
        self.assertFalse(response["ready"])
        self.assertIn("ambiguous_sku", {error["code"] for error in response["errors"]})
        self.assertIsNone(response["batch"])
        with self.assertRaises(opening.OpeningError) as raised:
            await self.finalize(batch)
        self.assertEqual(raised.exception.code, "opening_products_changed")
        self.assertEqual(await self.batch_count(), 1)
        self.assertEqual(await self.physical_snapshot(), before)

    async def test_changed_persisted_line_rejects_entire_batch_before_stock_writes(self):
        batch = await self.batch(csv_text=HEADER + "EXACT-SKU;2;10;ks\nSECOND-SKU;3;20;ks\n")
        async with self.sessions() as db:
            await db.execute(update(OpeningStockLine).where(OpeningStockLine.batch_id == batch["id"],
                OpeningStockLine.product_id == self.second_id).values(quantity=D("4"), value=D("80")))
            await db.commit()
        before = await self.physical_snapshot()
        with self.assertRaises(opening.OpeningError) as raised:
            await self.finalize(batch)
        self.assertEqual(raised.exception.code, "opening_preview_changed")
        self.assertEqual(await self.physical_snapshot(), before)

    async def test_finalize_is_atomic_and_006_rerun_never_changes_ledger(self):
        batch = await self.batch(csv_text=HEADER + "EXACT-SKU;2;10;ks\nSECOND-SKU;3;20;ks\n")
        result = await self.finalize(batch)
        self.assertEqual(result["movements_created"], 2)
        async with self.sessions() as db:
            balances = (await db.execute(select(StockBalance).order_by(StockBalance.product_id))).scalars().all()
            self.assertEqual([(b.qty_on_hand, b.avg_cost, b.total_value) for b in balances],
                             [(D("2"), D("10"), D("20")), (D("3"), D("20"), D("60"))])
            movements = (await db.execute(select(StockMovement))).scalars().all()
            self.assertTrue(all(row.movement_type == MovementType.INITIAL for row in movements))
            lines = (await db.execute(select(OpeningStockLine))).scalars().all()
            self.assertEqual({row.movement_id for row in lines}, {row.id for row in movements})
        before = await self.physical_snapshot()
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'SET search_path TO "{self.schema}"')
            await connection.execute((self.sql_root / "006_opening_stock.sql").read_text())
        finally:
            await connection.close()
        self.assertEqual(await self.physical_snapshot(), before)
        self.assertEqual(await self.finalize(batch), result)

    async def test_wrong_hash_changed_sku_and_expired_preview_never_write_stock(self):
        batch = await self.batch()
        before = await self.physical_snapshot()
        with self.assertRaises(opening.OpeningError):
            await self.finalize(batch, preview_hash="0" * 64)
        self.assertEqual(await self.physical_snapshot(), before)
        self.clock.return_value = NOW + timedelta(minutes=31)
        with self.assertRaises(opening.OpeningError):
            await self.finalize(batch)
        self.clock.return_value = NOW
        async with self.sessions() as db:
            await db.execute(update(Product).where(Product.id == self.first_id).values(sku="RENAMED-SKU"))
            await db.commit()
        renamed = await self.physical_snapshot()
        with self.assertRaises(opening.OpeningError):
            await self.finalize(batch)
        self.assertEqual(await self.physical_snapshot(), renamed)

    async def test_changed_or_inactive_warehouse_blocks_frozen_batch(self):
        batch = await self.batch()
        for values in ({"code": "WAREHOUSE-RENAMED"}, {"code": "opening-test", "is_active": False}):
            async with self.sessions() as db:
                await db.execute(update(Warehouse).where(Warehouse.id == self.warehouse_id).values(**values))
                await db.commit()
            before = await self.physical_snapshot()
            with self.assertRaises(opening.OpeningError):
                await self.finalize(batch)
            self.assertEqual(await self.physical_snapshot(), before)

    async def test_existing_zero_balance_blocks_whole_batch_and_rolls_back_new_sibling_balance(self):
        batch = await self.batch(csv_text=HEADER + "EXACT-SKU;2;10;ks\nSECOND-SKU;3;20;ks\n")
        async with self.sessions() as db:
            from stock_tracking_fixture import confirmed_inventory
            db.add(confirmed_inventory(self.second_id, self.warehouse_id))
            db.add(StockBalance(product_id=self.second_id, warehouse_id=self.warehouse_id))
            await db.commit()
        before = await self.physical_snapshot()
        with self.assertRaises(opening.OpeningError):
            await self.finalize(batch)
        self.assertEqual(await self.physical_snapshot(), before)

    async def test_unconfirmed_history_without_balance_can_start_new_count_without_deleting_history(self):
        batch = await self.batch()
        async with self.sessions() as db:
            db.add(StockMovement(idempotency_key="fixture-history", product_id=self.first_id,
                warehouse_id=self.warehouse_id, movement_type=MovementType.INITIAL,
                quantity=D("1"), unit_cost=D("5"), balance_after=D("1"), avg_cost_after=D("5")))
            await db.commit()
        before = await self.physical_snapshot()
        result = await self.finalize(batch)
        self.assertEqual(result["movements_created"], 1)
        after = await self.physical_snapshot()
        self.assertEqual(after["stock_movements"][:1], before["stock_movements"])

    async def run_held_opening(self, first_batch, second_call):
        entered, release = asyncio.Event(), asyncio.Event()
        original = opening.lock_stock_balances

        async def held(*args, **kwargs):
            result = await original(*args, **kwargs)
            if not entered.is_set():
                entered.set()
                await asyncio.wait_for(release.wait(), 5)
            return result

        with patch.object(opening, "lock_stock_balances", held):
            first = asyncio.create_task(self.finalize(first_batch))
            await asyncio.wait_for(entered.wait(), 5)
            second = asyncio.create_task(second_call())
            try:
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(second), 0.1)
            finally:
                release.set()
            return await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 5)

    async def test_same_batch_concurrent_confirmations_return_one_initial_movement_and_equal_result(self):
        batch = await self.batch()
        first, second = await self.run_held_opening(batch, lambda: self.finalize(batch))
        self.assertIsInstance(first, dict)
        self.assertEqual(second, first)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)), 1)

    async def test_different_batches_competing_for_same_product_only_one_can_commit(self):
        first_batch, second_batch = await self.batch(), await self.batch()
        first, second = await self.run_held_opening(first_batch, lambda: self.finalize(second_batch))
        self.assertIsInstance(first, dict)
        self.assertIsInstance(second, opening.OpeningError)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)), 1)
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockBalance)), 1)
            stored = await db.get(OpeningStockBatch, second_batch["id"])
            self.assertEqual(stored.status, "prepared")

    async def test_opening_wins_then_receipt_adds_weighted_cost_and_opening_replay_is_original(self):
        batch = await self.batch()
        receipt_id = await self.make_receipt()
        opening_result, receipt_result = await self.run_held_opening(batch, lambda: self.receive(receipt_id))
        self.assertIsInstance(opening_result, dict)
        self.assertIsInstance(receipt_result, dict)
        async with self.sessions() as db:
            balance = await db.scalar(select(StockBalance).where(StockBalance.product_id == self.first_id))
            self.assertEqual((balance.qty_on_hand, balance.avg_cost, balance.total_value), (D("5"), D("16"), D("80")))
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)), 2)
        self.assertEqual(await self.finalize(batch), opening_result)

    async def test_receipt_wins_then_opening_rejects_without_orphan_balance_or_extra_movement(self):
        batch = await self.batch(csv_text=HEADER + "EXACT-SKU;2;10;ks\nSECOND-SKU;1;5;ks\n")
        receipt_id = await self.make_receipt()
        entered, release = asyncio.Event(), asyncio.Event()
        original = receiving._write_stock_for_line

        async def held(*args, **kwargs):
            result = await original(*args, **kwargs)
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
            return result

        with patch.object(receiving, "_write_stock_for_line", held):
            receipt = asyncio.create_task(self.receive(receipt_id))
            await asyncio.wait_for(entered.wait(), 5)
            initial = asyncio.create_task(self.finalize(batch))
            try:
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(initial), 0.1)
            finally:
                release.set()
            results = await asyncio.wait_for(asyncio.gather(receipt, initial, return_exceptions=True), 5)
        self.assertIsInstance(results[0], dict)
        self.assertIsInstance(results[1], opening.OpeningError)
        async with self.sessions() as db:
            balances = (await db.execute(select(StockBalance))).scalars().all()
            self.assertEqual(len(balances), 1)
            self.assertEqual((balances[0].product_id, balances[0].qty_on_hand, balances[0].total_value),
                             (self.first_id, D("3"), D("60")))
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)), 1)
