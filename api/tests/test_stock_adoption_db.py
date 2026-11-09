"""An owner-adopted baseline preserves provenance, unknown cost and UUID recovery."""
import unittest
from decimal import Decimal

from sqlalchemy import select, func
import test_fifo_db as fifo_fixture
import test_stock_adjustments_db as fixture
from inventory_hub.db_models_ext import StockBalance, StockMovement
from inventory_hub.fifo_models import FifoLayer
from inventory_hub.stock_adjustment_models import StockAdjustment
from inventory_hub.stock_tracking_models import StockTracking
from inventory_hub.stock_adjustment_types import StockAdjustmentBatchPreview, StockAdjustmentBatchApply
from inventory_hub.services import fifo, stock_adjustments as service


@unittest.skipUnless(fifo_fixture.TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class StockAdoptionDatabaseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.StockAdjustmentDatabaseTests.asyncSetUp
    asyncTearDown = fixture.StockAdjustmentDatabaseTests.asyncTearDown
    transaction = fixture.StockAdjustmentDatabaseTests.transaction
    payload = fixture.StockAdjustmentDatabaseTests.payload
    prepare = fixture.StockAdjustmentDatabaseTests.prepare
    apply = fixture.StockAdjustmentDatabaseTests.apply
    seed_legacy = fifo_fixture.FifoDatabaseTests.seed_legacy

    async def test_adoption_replaces_legacy_quantity_without_inventing_count_or_cost(self):
        await self.seed_legacy(sku="SKU-A", quantity="100", cost="99", confirmed=False)
        result = await self.apply(await self.prepare(self.payload("5", evidence_kind="owner_adoption",
            source_reference="Owner source conflict rule", reason="Owner adopted opening; inventory count will follow")))
        self.assertEqual(result["counted_quantity"], "5")
        self.assertEqual(result["evidence_kind"], "owner_adoption")
        async with self.transaction() as db:
            tracking = await db.get(StockTracking, (self.products["SKU-A"], self.warehouse_id))
            self.assertEqual(tracking.source_type, "stock_adoption")
            self.assertEqual(Decimal(tracking.archived_balance["qty_on_hand"]), Decimal(100))
            layer = await db.scalar(select(FifoLayer).where(FifoLayer.product_id == self.products["SKU-A"]))
            self.assertEqual(layer.provenance["kind"], "owner_adoption")
            self.assertIsNone(layer.unit_cost)
            self.assertEqual(layer.quantity_remaining, Decimal(5))
            movements = (await db.scalars(select(StockMovement).where(
                StockMovement.product_id == self.products["SKU-A"]).order_by(StockMovement.id))).all()
            self.assertEqual([item.quantity for item in movements], [Decimal(100), Decimal(-100), Decimal(5)])

    async def test_old_physical_count_request_hash_stays_recoverable(self):
        payload = self.payload("3")
        prepared = await self.prepare(payload)
        old_raw = payload.model_dump(mode="json")
        old_raw.pop("evidence_kind")
        async with self.transaction() as db:
            row = await db.get(StockAdjustment, prepared["id"])
            self.assertEqual(row.request_hash, fifo.digest(old_raw))
        self.assertEqual(await self.prepare(payload), prepared)

    async def test_partial_batch_recovers_original_uuids_without_double_stock(self):
        previews = StockAdjustmentBatchPreview(entries=[
            self.payload("3", evidence_kind="owner_adoption"),
            self.payload("0", sku="LEGACY", evidence_kind="owner_adoption"),
        ])
        async with self.transaction() as db:
            prepared = await service.preview_batch(db, previews)
        self.assertTrue(prepared["ready"], prepared)
        entries = [{"request_id": item["request_id"], "preview_hash": item["preview"]["preview_hash"],
                    "confirmed": True, "quantities_verified": True, "costs_documented": True}
                   for item in prepared["items"]]
        wrong = [entries[0], {**entries[1], "preview_hash": "f" * 64}]
        async with self.transaction() as db:
            partial = await service.apply_batch(db, StockAdjustmentBatchApply(entries=wrong))
        self.assertFalse(partial["completed"])
        self.assertEqual([row["status"] for row in partial["items"]], ["completed", "blocked"])
        async with self.transaction() as db:
            recovered = await service.apply_batch(db, StockAdjustmentBatchApply(entries=entries))
        self.assertTrue(recovered["completed"])
        self.assertEqual(recovered["items"][0]["result"], partial["items"][0]["result"])
        self.assertTrue(recovered["items"][1]["result"]["initial_zero_count"])
        self.assertEqual(recovered["items"][1]["result"]["evidence_kind"], "owner_adoption")
        async with self.transaction() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement).where(
                StockMovement.reference_type == "stock_adjustment")), 1)
            self.assertEqual(await db.scalar(select(StockBalance.qty_on_hand).where(
                StockBalance.product_id == self.products["SKU-A"])), Decimal(3))
            zero = await db.get(StockTracking, (self.products["LEGACY"], self.warehouse_id))
            self.assertEqual(zero.source_type, "stock_adoption")
