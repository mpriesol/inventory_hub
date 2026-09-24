"""Recounts preserve FIFO costs, reservations and request identity in isolated PostgreSQL."""
import unittest
from pathlib import Path
from decimal import Decimal
from uuid import uuid4
import asyncpg
from sqlalchemy import select, func
import test_fifo_db as fixture
from inventory_hub.db_models import MovementType
from inventory_hub.db_models_ext import ShopProduct, StockBalance, StockMovement
from inventory_hub.fifo_models import FifoAllocation, FifoLayer, FifoState
from inventory_hub.order_stock_models import OrderStockPolicy
from inventory_hub.stock_adjustment_models import StockAdjustment
from inventory_hub.stock_adjustment_types import StockAdjustmentPreview, StockAdjustmentApply
from inventory_hub.services import stock_adjustments as service, fifo
from inventory_hub.services import stock_projection


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

    async def test_explicit_initial_zero_enables_projection_without_fake_receipt(self):
        async with self.transaction() as db:
            db.add(OrderStockPolicy(shop_id=self.shop_id, warehouse_id=self.warehouse_id, starts_at=fixture.NOW,
                revision=1, status_actions={}, status_hash="a" * 64, statuses=[]))
            db.add(ShopProduct(shop_id=self.shop_id, product_id=self.products["SKU-A"], external_code="xTrek",
                parent_code="xTrek", variant_code="SKU-A", is_variant=True))
        prepared = await self.prepare(self.payload("0"))
        self.assertTrue(prepared["preview"]["initial_zero_count"])
        self.assertIsNone(prepared["preview"]["before_quantity"])
        async with self.transaction() as db:
            projected = await stock_projection.preview(db, "biketrek", ["SKU-A"])
            self.assertFalse(projected["rows"][0]["quantity_known"])
        result = await self.apply(prepared)
        self.assertTrue(result["initial_zero_count"])
        self.assertIsNone(result["movement_id"])
        self.assertEqual((result["counted_quantity"], result["delta"]), ("0", "0"))
        self.assertEqual(await self.apply(prepared), result)
        async with self.transaction() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(FifoLayer)), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockAdjustment)), 1)
            state = await db.get(FifoState, (self.products["SKU-A"], self.warehouse_id))
            self.assertEqual(state.activation_kind, "initial_zero_count")
            projected = await stock_projection.preview(db, "biketrek", ["SKU-A"])
            self.assertTrue(projected["rows"][0]["quantity_known"])
            self.assertEqual(projected["rows"][0]["qty_available"], "0")
            balance = await db.scalar(select(StockBalance).where(StockBalance.product_id == self.products["SKU-A"]))
            self.assertIsNone(balance.last_movement_id)
            self.assertIsNone(balance.last_purchase_price)
        with self.assertRaisesRegex(fifo.FifoError, "no_change"):
            await self.prepare(self.payload("0"))
        # A subsequent genuine receipt retains its actual cost instead of a fictitious zero layer.
        receipt = await self.apply(await self.prepare(self.payload("2", cost_status="known", unit_cost="12")))
        self.assertFalse(receipt["initial_zero_count"])
        self.assertEqual(Decimal(receipt["valuation"]["total_value"]), Decimal("24"))

    async def test_existing_unverified_zero_can_be_counted_but_only_once(self):
        async with self.transaction() as db:
            db.add(StockBalance(product_id=self.products["SKU-A"], warehouse_id=self.warehouse_id,
                qty_on_hand=0, qty_reserved=0, qty_quarantined=0, avg_cost=None, total_value=None))
            db.add(FifoState(product_id=self.products["SKU-A"], warehouse_id=self.warehouse_id,
                revision=1, activated_at=fixture.NOW, activation_kind="receipt"))
        first = await self.prepare(self.payload("0"))
        second = await self.prepare(self.payload("0"))
        await self.apply(first)
        with self.assertRaisesRegex(fifo.FifoError, "preview_stale"):
            await self.apply(second)
        async with self.transaction() as db:
            state = await db.get(FifoState, (self.products["SKU-A"], self.warehouse_id))
            self.assertEqual(state.revision, 2)
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)), 0)

    async def test_initial_zero_rejects_unit_cost_and_racing_receipt(self):
        with self.assertRaisesRegex(fifo.FifoError, "issue_cost_derived"):
            await self.prepare(self.payload("0", cost_status="known", unit_cost="0"))
        zero = await self.prepare(self.payload("0"))
        await self.apply(await self.prepare(self.payload("1")))
        with self.assertRaisesRegex(fifo.FifoError, "preview_stale"):
            await self.apply(zero)

    async def test_zero_audit_never_certifies_a_later_unexplained_nonzero_balance(self):
        from inventory_hub.services.stock_evidence import physical_stock_evidence
        await self.apply(await self.prepare(self.payload("0")))
        async with self.transaction() as db:
            balance = await db.scalar(select(StockBalance).where(StockBalance.product_id == self.products["SKU-A"]))
            balance.qty_on_hand = Decimal(1)
        async with self.transaction() as db:
            self.assertFalse(await db.scalar(select(physical_stock_evidence()).select_from(StockBalance).where(
                StockBalance.product_id == self.products["SKU-A"], StockBalance.warehouse_id == self.warehouse_id)))
