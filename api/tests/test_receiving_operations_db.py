"""Receipt undo, cycle idempotency and reversible history on isolated PostgreSQL."""
import asyncio
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import asyncpg
from sqlalchemy import func, select, text
import test_receiving_db as fixture
from inventory_hub.db_models import MovementType, ReceivingStatus
from inventory_hub.db_models_ext import ReceivingSession, StockBalance, StockMovement
from inventory_hub.fifo_models import FifoLayer
from inventory_hub.fifo_return_types import FifoCostRevisionInput
from inventory_hub.receiving_operation_models import ReceivingOperation
from inventory_hub.receiving_operation_types import ReceivingOperationPreview, ReceivingOperationApply
from inventory_hub.services import fifo, fifo_returns, receiving_operations as service
from inventory_hub.routers import receiving_db as receiving


@unittest.skipUnless(fixture.TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class ReceivingOperationDatabaseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ReceivingDatabaseTests.asyncSetUp
    asyncTearDown = fixture.ReceivingDatabaseTests.asyncTearDown
    make_session = fixture.ReceivingDatabaseTests.make_session
    finalize = fixture.ReceivingDatabaseTests.finalize

    async def preview(self, session_id, action="reopen", request_id=None):
        async with self.sessions() as db:
            return await service.preview(db, "receipt-test", session_id, ReceivingOperationPreview(
                request_id=request_id or uuid4(), action=action, reason="Correct the test receipt"), "user:17")

    async def apply(self, preview):
        async with self.sessions() as db:
            return await service.apply(db, "receipt-test", preview["session_id"], preview["id"],
                ReceivingOperationApply(preview_hash=preview["preview_hash"], confirmed=True), "user:18")

    async def balance(self):
        async with self.sessions() as db:
            row = await db.scalar(select(StockBalance).where(StockBalance.product_id == self.product_id))
            return (row.qty_on_hand, row.qty_reserved, row.total_value, row.last_purchase_price) if row else None

    async def movements(self):
        async with self.sessions() as db:
            return list((await db.execute(text("SELECT * FROM stock_movements ORDER BY id"))).mappings())

    async def test_reopen_removes_exact_receipt_and_new_cycle_posts_once(self):
        first = await self.make_session()
        await self.finalize(first)
        second = await self.make_session([{"product_id": self.product_id, "received_qty": D(2), "unit_price": D(7)}])
        await self.finalize(second)
        before = await self.movements()
        preview = await self.preview(second)
        self.assertTrue(preview["ready"])
        self.assertEqual(D(preview["impacts"][0]["delta"]), D(-2))
        result = await self.apply(preview)
        self.assertEqual(result, await self.apply(preview))
        self.assertEqual((result["status"], result["posting_revision"]), ("paused", 1))
        self.assertEqual(await self.balance(), (D(2), D(0), D(10), D(5)))
        self.assertEqual((await self.movements())[:len(before)], before)
        async with self.sessions() as db:
            await receiving.resume_session("receipt-test", second, db)
            await db.commit()
        async with self.sessions() as db:
            with self.assertRaisesRegex(Exception, "receiving_posting_revision_changed"):
                await receiving.finalize_session("receipt-test", second, receiving.FinalizeRequest(), db)
        async with self.sessions() as db:
            await receiving.set_line_quantity("receipt-test", second,
                receiving.SetQtyRequest(line_index=0, received_qty="3"), db)
            await db.commit()
        async with self.sessions() as db:
            request = receiving.FinalizeRequest(force=True, expected_posting_revision=1)
            posted = await receiving.finalize_session("receipt-test", second, request, db)
        async with self.sessions() as db:
            self.assertEqual(posted, await receiving.finalize_session("receipt-test", second, request, db))
            session = await db.get(ReceivingSession, second)
            self.assertNotIn("receiving_reversed", session.session_data)
        self.assertEqual(await self.balance(), (D(5), D(0), D(31), D(7)))
        self.assertEqual(len(await self.movements()), 4)

    async def test_reverse_then_reopen_does_not_compensate_twice(self):
        identifier = await self.make_session()
        await self.finalize(identifier)
        reversed_result = await self.apply(await self.preview(identifier, "reverse"))
        self.assertEqual(reversed_result["status"], "cancelled")
        self.assertEqual(await self.balance(), (D(0), D(0), D(0), None))
        opened = await self.apply(await self.preview(identifier, "reopen"))
        self.assertEqual(opened["movement_ids"], [])
        self.assertEqual(opened["stock_effect"], "unchanged")
        self.assertEqual(len(await self.movements()), 2)

    async def test_pack_conversion_undo_uses_actual_stock_units(self):
        identifier = await self.make_session([{"product_id": self.product_id, "received_qty": D(2),
            "unit_price": D(6), "purchase_unit": "pack", "purchase_to_stock_factor": D(3),
            "conversion_stock_unit": "pcs"}])
        await self.finalize(identifier)
        preview = await self.preview(identifier, "reverse")
        self.assertEqual(D(preview["impacts"][0]["delta"]), D(-6))
        await self.apply(preview)
        self.assertEqual((await self.balance())[0], D(0))
        self.assertEqual([row["quantity"] for row in await self.movements()], [D(6), D(-6)])

    async def test_unknown_revised_cost_is_not_replaced_with_zero_during_undo(self):
        identifier = await self.make_session()
        await self.finalize(identifier)
        async with self.sessions() as db:
            layer_id = await db.scalar(select(FifoLayer.id))
            await fifo_returns.revise_cost(db, FifoCostRevisionInput(request_id=uuid4(), root_layer_id=layer_id,
                expected_revision=0, new_unit_cost=None, cost_status="unknown", reason="Cost evidence needs correction",
                document_reference="Test cost correction", confirmed=True))
        originals = await self.movements()
        await self.apply(await self.preview(identifier, "reverse"))
        movements = await self.movements()
        self.assertEqual(movements[:-1], originals)
        self.assertIsNone(movements[-1]["unit_cost"])
        self.assertIsNone(movements[-1]["total_cost"])
        self.assertEqual(await self.balance(), (D(0), D(0), D(0), None))

    async def test_reservations_and_consumed_receipt_layers_block_undo(self):
        identifier = await self.make_session()
        await self.finalize(identifier)
        async with self.sessions() as db:
            balance = await db.scalar(select(StockBalance).where(StockBalance.product_id == self.product_id,
                StockBalance.warehouse_id == self.warehouse_id))
            balance.qty_reserved = D(1)
            await db.commit()
        blocked = await self.preview(identifier)
        self.assertFalse(blocked["ready"])
        self.assertIn("receiving_reversal_committed_stock", str(blocked["errors"]))
        async with self.sessions() as db:
            balance = await db.scalar(select(StockBalance).where(StockBalance.product_id == self.product_id,
                StockBalance.warehouse_id == self.warehouse_id))
            balance.qty_reserved = D(0)
            await db.flush()
            plan = await fifo.plan_issue(db, balance, [{"line_key": "sale", "quantity": D(1)}], lock=True)
            movement = StockMovement(idempotency_key="test:issue", product_id=self.product_id,
                warehouse_id=self.warehouse_id, movement_type=MovementType.SALE_OUT, quantity=D(-1))
            await fifo.apply_issue(db, balance, movement, plan["allocations"])
            await db.commit()
        before = await self.movements()
        blocked = await self.preview(identifier)
        self.assertFalse(blocked["ready"])
        self.assertIn("receiving_reversal_used_stock", str(blocked["errors"]))
        with self.assertRaisesRegex(service.ReceivingOperationError, "blocked"):
            await self.apply(blocked)
        self.assertEqual(before, await self.movements())

    async def test_stock_change_invalidates_preview_and_new_review_is_required(self):
        identifier = await self.make_session()
        await self.finalize(identifier)
        preview = await self.preview(identifier)
        await self.finalize(await self.make_session())
        before = await self.movements()
        with self.assertRaisesRegex(service.ReceivingOperationError, "stale"):
            await self.apply(preview)
        self.assertEqual(before, await self.movements())
        await self.apply(await self.preview(identifier))
        self.assertEqual((await self.balance())[0], D(2))

    async def test_archive_restore_preserves_posted_and_unposted_stock_and_default_list(self):
        for completed in (False, True):
            identifier = await self.make_session()
            if completed:
                await self.finalize(identifier)
            before = await self.movements()
            before_balance = await self.balance()
            archived = await self.apply(await self.preview(identifier, "archive"))
            self.assertTrue(archived["archived"])
            self.assertEqual(archived["stock_effect"], "unchanged")
            async with self.sessions() as db:
                visible = await receiving.list_sessions("receipt-test", limit=100, db=db)
                all_rows = await receiving.list_sessions("receipt-test", limit=100, db=db, include_archived=True)
                self.assertNotIn(identifier, [row["session_id"] for row in visible])
                self.assertIn(identifier, [row["session_id"] for row in all_rows])
                receipt = await db.get(ReceivingSession, identifier)
                active = await receiving.get_active_session("receipt-test", receipt.invoice_number, db)
                closed = await receiving.get_active_session("receipt-test", receipt.invoice_number, db, include_closed=True)
                self.assertFalse(active["has_session"])
                self.assertTrue(closed["session"]["archived"])
                with self.assertRaisesRegex(Exception, "receiving_archived"):
                    await receiving.finalize_session("receipt-test", identifier, receiving.FinalizeRequest(), db)
            restored = await self.apply(await self.preview(identifier, "restore"))
            self.assertFalse(restored["archived"])
            self.assertEqual(restored["status"], "completed" if completed else "in_progress")
            self.assertEqual(before, await self.movements())
            self.assertEqual(before_balance, await self.balance())
            if completed:
                await self.finalize(identifier)
                self.assertEqual(before, await self.movements())

    async def test_same_request_and_concurrent_apply_never_reverse_twice(self):
        identifier = await self.make_session()
        await self.finalize(identifier)
        request_id = uuid4()
        preview = await self.preview(identifier, request_id=request_id)
        self.assertEqual(preview, await self.preview(identifier, request_id=request_id))
        with self.assertRaisesRegex(service.ReceivingOperationError, "request_conflict"):
            await self.preview(identifier, action="archive", request_id=request_id)
        first, second = await asyncio.gather(self.apply(preview), self.apply(preview))
        self.assertEqual(first, second)
        self.assertEqual(len(first["movement_ids"]), 1)
        self.assertEqual(len(await self.movements()), 2)

    async def test_history_has_completed_scoped_summaries_without_private_snapshots(self):
        identifier = await self.make_session()
        unrelated = await self.make_session()
        await self.preview(unrelated, "archive")
        await self.preview(identifier, "archive")
        first = await self.apply(await self.preview(identifier, "archive"))
        await self.apply(await self.preview(identifier, "restore"))
        async with self.sessions() as db:
            history = await service.history(db, "receipt-test", identifier)
            self.assertEqual(history["limit"], 50)
            self.assertEqual([item["action"] for item in history["operations"]], ["restore", "archive"])
            self.assertEqual(history["operations"][-1]["id"], first["id"])
            self.assertEqual(history["operations"][-1]["actor"], "user:18")
            self.assertEqual(set(history["operations"][0]),
                {"id", "action", "reason", "actor", "at", "stock_effect", "impacts", "status", "archived"})
            self.assertEqual((await service.history(db, "receipt-test", unrelated))["operations"], [])
            with self.assertRaisesRegex(service.ReceivingOperationError, "not_found"):
                await service.history(db, "not-this-supplier", identifier)

    async def test_partial_failure_rolls_back_and_completed_audit_is_immutable(self):
        identifier = await self.make_session([
            {"product_id": self.product_id, "received_qty": D(2), "unit_price": D(5)},
            {"product_id": self.product_id, "received_qty": D(2), "unit_price": D(7)}])
        await self.finalize(identifier)
        before, balance_before = await self.movements(), await self.balance()
        preview = await self.preview(identifier)
        original, count = fifo.apply_issue, 0
        async def fail_second(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError("synthetic failure")
            return await original(*args, **kwargs)
        with patch.object(fifo, "apply_issue", fail_second):
            with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                await self.apply(preview)
        self.assertEqual(before, await self.movements())
        self.assertEqual(balance_before, await self.balance())
        result = await self.apply(preview)
        async with self.sessions() as db:
            with self.assertRaises(Exception):
                await db.execute(text("UPDATE receiving_operations SET actor='changed' WHERE id=:id"), {"id": preview["id"]})
            await db.rollback()
        connection = await asyncpg.connect(fixture.TEST_URL)
        try:
            await connection.execute(f'SET search_path TO "{self.schema}"')
            await connection.execute((Path(__file__).resolve().parents[2] / "infra/db-init/052_receiving_operations.sql").read_text())
        finally:
            await connection.close()
        self.assertEqual(result, await self.apply(preview))

    async def test_archived_invoice_must_be_restored_before_receipt(self):
        identifier = await self.make_session()
        await self.apply(await self.preview(identifier, "archive"))
        async with self.sessions() as db:
            session = await db.get(ReceivingSession, identifier)
            await db.execute(text("""INSERT INTO uploaded_invoices
                (supplier_id,invoice_number,original_filename,stored_filename,file_path,file_type,receiving_session_id,archived_at)
                VALUES (:supplier,:number,'test.pdf','test.pdf','test.pdf','pdf',:session,now())"""),
                {"supplier": self.supplier_id, "number": session.invoice_number, "session": identifier})
            await db.commit()
        with self.assertRaisesRegex(service.ReceivingOperationError, "invoice_archived"):
            await self.preview(identifier, "restore")
        async with self.sessions() as db:
            await db.execute(text("UPDATE uploaded_invoices SET archived_at=NULL,processing_status='processed_without_stock'"))
            await db.commit()
        with self.assertRaisesRegex(service.ReceivingOperationError, "invoice_processed_without_stock"):
            await self.preview(identifier, "restore")
        async with self.sessions() as db:
            await db.execute(text("UPDATE uploaded_invoices SET processing_status='unprocessed'"))
            await db.commit()
        result = await self.apply(await self.preview(identifier, "restore"))
        self.assertEqual(result["movement_ids"], [])
