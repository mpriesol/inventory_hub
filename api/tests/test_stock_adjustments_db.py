"""Recounts preserve FIFO costs, reservations and request identity in isolated PostgreSQL."""
import unittest
from pathlib import Path
from decimal import Decimal
from uuid import uuid4
import asyncpg
from sqlalchemy import select, func
import test_fifo_db as fixture
from inventory_hub.db_models import MovementType
from inventory_hub.db_models_ext import StockBalance, StockMovement
from inventory_hub.fifo_models import FifoAllocation, FifoLayer
from inventory_hub.stock_adjustment_types import StockAdjustmentPreview, StockAdjustmentApply
from inventory_hub.services import stock_adjustments as service, fifo


@unittest.skipUnless(fixture.TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class StockAdjustmentDatabaseTests(unittest.IsolatedAsyncioTestCase):
    transaction = fixture.FifoDatabaseTests.transaction
    open_stock = fixture.FifoDatabaseTests.open_stock
    asyncTearDown = fixture.FifoDatabaseTests.asyncTearDown

    async def asyncSetUp(self):
        await fixture.FifoDatabaseTests.asyncSetUp(self)
        connection = await asyncpg.connect(fixture.TEST_URL)
        try:
            await connection.execute(f'SET search_path TO "{self.schema}"')
            sql = (Path(__file__).resolve().parents[2] / "infra/db-init/017_stock_adjustments.sql").read_text()
            await connection.execute(sql)
            await connection.execute(sql)
        finally:
            await connection.close()

    def payload(self, count="3", **changes):
        return StockAdjustmentPreview(**{"request_id": uuid4(), "sku": "SKU-A", "warehouse_code": "fifo-central",
            "counted_quantity": count, "counted_at": fixture.NOW, "source_reference": "RECOUNT-1",
            "operator_name": "Test operator", "reason": "Count corrected", **changes})

    async def prepare(self, payload):
        async with self.transaction() as db:
            return await service.preview(db, payload)

    async def apply(self, prepared):
        async with self.transaction() as db:
            return await service.apply(db, prepared["id"], StockAdjustmentApply(preview_hash=prepared["preview_hash"],
                confirmed=True, quantities_verified=True, costs_documented=True))

    async def test_decrease_uses_fifo_and_replay_has_one_movement(self):
        await self.open_stock(quantity="5", cost="10")
        before = await self.prepare(self.payload())
        result = await self.apply(before)
        self.assertEqual(result["delta"], "-2")
        self.assertEqual(await self.apply(before), result)
        async with self.transaction() as db:
            movement = await db.get(StockMovement, result["movement_id"])
            self.assertEqual(movement.movement_type, MovementType.ADJUSTMENT_OUT)
            self.assertEqual(movement.total_cost, Decimal("20"))
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement).where(
                StockMovement.reference_type == "stock_adjustment")), 1)
            self.assertEqual(await db.scalar(select(FifoAllocation.quantity).where(
                FifoAllocation.issue_movement_id == movement.id)), Decimal("2"))
            self.assertEqual(await db.scalar(select(FifoLayer.quantity_remaining).where(
                FifoLayer.product_id == self.products["SKU-A"])), Decimal("3"))

    async def test_pristine_increase_retains_unknown_cost(self):
        result = await self.apply(await self.prepare(self.payload()))
        self.assertIsNone(result["valuation"]["total_value"])
        async with self.transaction() as db:
            movement = await db.get(StockMovement, result["movement_id"])
            self.assertEqual(movement.movement_type, MovementType.ADJUSTMENT_IN)
            self.assertIsNone(movement.unit_cost)
            self.assertEqual(movement.balance_after, Decimal("3"))

    async def test_increase_known_cost_keeps_old_receipt(self):
        await self.open_stock(quantity="2", cost="10")
        result = await self.apply(await self.prepare(self.payload("5", cost_status="known", unit_cost="20")))
        self.assertEqual(Decimal(result["valuation"]["total_value"]), Decimal("80"))
        async with self.transaction() as db:
            self.assertEqual(list((await db.scalars(select(FifoLayer.quantity_remaining).where(
                FifoLayer.product_id == self.products["SKU-A"]).order_by(FifoLayer.id))).all()), [Decimal(2), Decimal(3)])

    async def test_changed_reservations_invalidate_preview(self):
        await self.open_stock(quantity="5", cost="10")
        preview = await self.prepare(self.payload())
        async with self.transaction() as db:
            balance = await db.scalar(select(StockBalance).where(StockBalance.product_id == self.products["SKU-A"]))
            balance.qty_reserved = Decimal(1)
        with self.assertRaisesRegex(fifo.FifoError, "preview_stale"):
            await self.apply(preview)

    async def test_count_below_commitments_rejected_without_movement(self):
        await self.open_stock(quantity="5", cost="10")
        async with self.transaction() as db:
            balance = await db.scalar(select(StockBalance).where(StockBalance.product_id == self.products["SKU-A"]))
            balance.qty_reserved = Decimal(4)
        with self.assertRaisesRegex(fifo.FifoError, "below_committed"):
            await self.prepare(self.payload())

    async def test_request_id_cannot_be_reused_for_a_different_count(self):
        payload = self.payload()
        first = await self.prepare(payload)
        self.assertEqual(await self.prepare(payload), first)
        with self.assertRaisesRegex(fifo.FifoError, "request_conflict"):
            await self.prepare(payload.model_copy(update={"counted_quantity": "4"}))
