"""Cost projection against real FIFO receipts/issues in isolated PostgreSQL."""
import unittest
from decimal import Decimal
from uuid import uuid4

import test_fifo_db as fifo_fixture
from inventory_hub.db_models_ext import ShopOrder, StockMovement
from inventory_hub.fifo_return_types import FifoCostRevisionInput, FifoReturnInput
from inventory_hub.services import fifo_cost_projection as service
from inventory_hub.services import fifo_returns


@unittest.skipUnless(fifo_fixture.TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class FifoCostProjectionDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Reuse guarded fixture helpers without inheriting/re-running its test cases.
        self.fixture = fifo_fixture.FifoDatabaseTests()
        await self.fixture.asyncSetUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def product(self, sku="SKU-A"):
        async with self.fixture.sessions() as db:
            return await service.product_cost(db, self.fixture.products[sku], self.fixture.warehouse_id)

    async def order(self, identifier):
        async with self.fixture.sessions() as db:
            return await service.order_cost(db, identifier)

    async def test_real_receipts_reservation_issue_and_replenishment_publish_next_layer(self):
        for cost in ("10", "20", "30", "40", "50"):
            await self.fixture.receive(cost=cost)
        before = await self.product()
        observed = self.fixture.order(self.fixture.line(quantity="3.000"))
        await self.fixture.apply_order(observed, "reserve")
        self.assertEqual((await self.product())["unit_cost"], "10")
        issued = await self.fixture.apply_order(observed)
        snapshot = await self.fixture.physical_snapshot()
        sale, remaining = await self.order(issued["order_id"]), await self.product()
        self.assertEqual((before["unit_cost"], sale["total_cost"], sale["lines"][0]["unit_cost"], remaining["unit_cost"]),
                         ("10", "60", "20", "40"))
        self.assertEqual(await self.fixture.physical_snapshot(), snapshot)
        self.assertEqual((await self.fixture.valuation())[0].avg_cost, Decimal("45"))
        await self.fixture.apply_order(self.fixture.order(self.fixture.line(quantity="2")))
        with self.assertRaises(service.FifoCostProjectionError) as raised:
            await self.product()
        self.assertEqual(raised.exception.code, "fifo_cost_no_available_layer")
        await self.fixture.receive(cost="75")
        self.assertEqual((await self.product())["unit_cost"], "75")

    async def test_duplicate_sku_rows_keep_remote_line_uuid_and_distinct_cost(self):
        for cost in ("10", "20", "30"):
            await self.fixture.receive(cost=cost)
        first, second = sorted([str(uuid4()), str(uuid4())])
        issued = await self.fixture.apply_order(self.fixture.order(
            self.fixture.line(quantity="1", line_key=first), self.fixture.line(quantity="2", line_key=second)))
        result = await self.order(issued["order_id"])
        self.assertEqual([(line["line_key"], line["unit_cost"]) for line in result["lines"]], [(first, "10"), (second, "25")])

    async def test_old_issued_order_reprice_uses_documented_revision_preserves_movement_and_returns(self):
        for cost in ("10", "20", "30"):
            await self.fixture.receive(cost=cost)
        issued = await self.fixture.apply_order(self.fixture.order(self.fixture.line(quantity="3")))
        initial = await self.order(issued["order_id"])
        root_id = initial["lines"][0]["allocations"][0]["root_layer_id"]
        async with self.fixture.transaction() as db:
            await fifo_returns.revise_cost(db, FifoCostRevisionInput(request_id=uuid4(), root_layer_id=root_id,
                expected_revision=0, new_unit_cost="40", cost_status="known", reason="Verified invoice correction",
                document_reference="CORRECTION-1", confirmed=True))
        revised = await self.order(issued["order_id"])
        self.assertEqual((revised["total_cost"], revised["lines"][0]["unit_cost"]), ("90", "30"))
        self.assertNotEqual(initial["signature"], revised["signature"])
        async with self.fixture.transaction() as db:
            movement = await db.get(StockMovement, issued["movement_ids"][0])
            self.assertEqual(movement.total_cost, Decimal("60"))
            await fifo_returns.receive_return(db, FifoReturnInput(request_id=uuid4(), issue_movement_id=movement.id,
                quantity="1", case_reference="RETURN-1", reason="Physical return verified", condition="good",
                confirmed=True, physical_received=True))
        self.assertEqual(await self.order(issued["order_id"]), revised)

    async def test_legacy_issue_with_numeric_cost_is_not_invented_fifo_evidence(self):
        await self.fixture.seed_legacy()
        issued = await self.fixture.apply_order(self.fixture.order(self.fixture.line(sku="LEGACY")))
        with self.assertRaises(service.FifoCostProjectionError) as raised:
            await self.order(issued["order_id"])
        self.assertEqual(raised.exception.code, "fifo_cost_allocations_missing")

    async def test_order_wrapper_is_scoped_and_projection_is_repeatable_without_writes(self):
        await self.fixture.receive(cost="10")
        issued = await self.fixture.apply_order(self.fixture.order())
        async with self.fixture.sessions() as db:
            order = await db.get(ShopOrder, issued["order_id"])
            value = await service.order_cost_projection(db, order.shop_id, order.external_id, self.fixture.warehouse_id)
            self.assertEqual(value, await service.order_cost(db, order.id))
            self.assertFalse(db.new or db.dirty or db.deleted)
            with self.assertRaises(service.FifoCostProjectionError):
                await service.order_cost_projection(db, order.shop_id, order.external_id, self.fixture.warehouse_id + 999)
