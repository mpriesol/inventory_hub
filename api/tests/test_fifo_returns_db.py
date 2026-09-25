"""Physical return and cost propagation regression tests in guarded PostgreSQL."""
import asyncio
import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.db_models import MovementType, Product, Shop, Warehouse
from inventory_hub.db_models_ext import StockBalance, StockMovement
from inventory_hub.fifo_models import FifoAllocation, FifoLayer
from inventory_hub.fifo_return_models import FifoCostRevision, FifoRelease, FifoReturn, FifoReturnLine
from inventory_hub.fifo_return_types import FifoCostRevisionInput, FifoReleaseInput, FifoReturnInput
from inventory_hub.routers import stock
from inventory_hub.services import fifo
from inventory_hub.services import fifo_returns as service
from inventory_hub.services.stock_balances import lock_stock_balances
from inventory_hub.services.stock_publication_gate import StockPublicationHoldError
from inventory_hub.stock_publication_models import StockPublicationHold


TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")
NOW = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
D = Decimal


@unittest.skipUnless(TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class FifoReturnsDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("FIFO returns tests require a dedicated localhost *_catalog_test database")
        self.schema = "fifo_return_test_" + uuid4().hex
        sql_root = Path(__file__).resolve().parents[2] / "infra" / "db-init"
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            await connection.execute((sql_root / "001_schema.sql").read_text())
            await connection.execute((sql_root / "019_stock_tracking.sql").read_text())
            await connection.execute("CREATE TYPE payment_status AS ENUM ('unpaid', 'partial', 'paid')")
            for filename in ("002_invoice_management.sql", "004_shop_product_content.sql", "006_opening_stock.sql", "007_order_stock.sql",
                             "008_order_collection.sql", "009_stock_automation.sql", "010_stock_publication.sql", "011_fifo.sql",
                             "012_product_editor.sql"):
                await connection.execute((sql_root / filename).read_text())
        finally:
            await connection.close()
        self.engine = create_async_engine(TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1),
            poolclass=NullPool, connect_args={"server_settings": {"search_path": self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        async with self.sessions() as db:
            product = Product(sku="FIFO-RETURN-SKU", name="Physical return test")
            warehouses = [Warehouse(code="fifo-returns-a", name="Returns A"), Warehouse(code="fifo-returns-b", name="Returns B")]
            db.add_all([product, *warehouses])
            await db.flush()
            self.product_id = product.id
            self.warehouse_a, self.warehouse_b = [warehouse.id for warehouse in warehouses]
            from stock_tracking_fixture import confirmed_inventory
            db.add_all([confirmed_inventory(product.id, warehouse.id) for warehouse in warehouses])
            self.shop_id = await db.scalar(select(Shop.id).where(Shop.code == "biketrek"))
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            await connection.close()

    async def invoke(self, name, payload):
        async with self.sessions() as db:
            try:
                return await getattr(service, name)(db, payload)
            except BaseException:
                await db.rollback()
                raise

    async def receive(self, quantity="10", cost=None):
        async with self.sessions() as db:
            rows, _ = await lock_stock_balances(db, {self.product_id}, self.warehouse_a)
            balance = rows[self.product_id]
            balance.qty_on_hand += D(quantity)
            price = None if cost is None else D(cost)
            existing_layers = list((await db.scalars(select(FifoLayer).where(
                FifoLayer.product_id == self.product_id, FifoLayer.warehouse_id == self.warehouse_a))).all())
            from types import SimpleNamespace
            preview = fifo.summarize([*existing_layers, SimpleNamespace(quantity_remaining=D(quantity),
                unit_cost=price, cost_status="unknown" if price is None else "known", stock_status="available")])
            movement = StockMovement(idempotency_key="test-receipt-" + uuid4().hex, product_id=self.product_id,
                warehouse_id=self.warehouse_a, movement_type=MovementType.RECEIVING_IN, quantity=D(quantity),
                unit_cost=price, total_cost=None if price is None else D(quantity) * price,
                balance_after=balance.qty_on_hand,
                avg_cost_after=None if preview["avg_cost"] is None else D(preview["avg_cost"]))
            db.add(movement)
            await db.flush()
            layer = await fifo.add_receipt(db, balance, movement, NOW, price,
                "unknown" if price is None else "known", {"kind": "documented_receipt", "source_reference": "synthetic"})
            balance.last_purchase_price, balance.last_purchase_at = price, NOW
            await db.commit()
            return layer.id, movement.id

    async def issue(self, quantity="6", warehouse_id=None):
        wid = warehouse_id or self.warehouse_a
        async with self.sessions() as db:
            rows, _ = await lock_stock_balances(db, {self.product_id}, wid, create_missing=False)
            balance = rows[self.product_id]
            plan = await fifo.plan_issue(db, balance, [{"line_key": "test-line", "quantity": quantity}], lock=True)
            movement = StockMovement(idempotency_key="test-issue-" + uuid4().hex, product_id=self.product_id,
                warehouse_id=wid, movement_type=MovementType.SALE_OUT, quantity=-D(quantity),
                balance_after=balance.qty_on_hand-D(quantity), avg_cost_after=None,
                reference_type="order_stock", reference_id="synthetic-order")
            await fifo.apply_issue(db, balance, movement, plan["allocations"])
            await db.commit()
            return movement.id

    def return_request(self, issue_id, quantity="2", condition="good", request_id=None):
        return FifoReturnInput(request_id=request_id or uuid4(), issue_movement_id=issue_id,
            quantity=quantity, case_reference="RMA-SYNTHETIC-1", reason="Verified physical return",
            condition=condition, confirmed=True, physical_received=True)

    def release_request(self, layer_id, quantity="1", warehouse_id=None):
        return FifoReleaseInput(request_id=uuid4(), source_layer_id=layer_id, quantity=quantity,
            target_warehouse_id=warehouse_id or self.warehouse_a, reason="Condition checked",
            confirmed=True, condition_verified=True)

    def cost_request(self, root_id, cost="7.50", status="known", revision=0):
        return FifoCostRevisionInput(request_id=uuid4(), root_layer_id=root_id, expected_revision=revision,
            new_unit_cost=cost, cost_status=status, reason="Acquisition invoice verified",
            document_reference="INV-SYNTHETIC-1", confirmed=True)

    async def movements(self):
        async with self.sessions() as db:
            return (await db.execute(text("SELECT * FROM stock_movements ORDER BY id"))).mappings().all()

    async def test_unknown_issue_partial_return_transfer_reissue_invoice_cost_conserves_value(self):
        root_id, _ = await self.receive()
        issue_id = await self.issue()
        returned = await self.invoke("receive_return", self.return_request(issue_id))
        self.assertEqual(returned["stock_status"], "quarantine")
        self.assertIsNone(returned["valuation"]["total_value"])
        self.assertEqual(D(returned["valuation"]["quarantined_qty"]), D("2"))
        quarantine_id = returned["lines"][0]["layer_id"]
        released_local = await self.invoke("release_quarantine", self.release_request(quarantine_id))
        released_other = await self.invoke("release_quarantine", self.release_request(quarantine_id, warehouse_id=self.warehouse_b))
        self.assertEqual(released_local["physical_received_at"], returned["received_at"])
        self.assertEqual(released_other["root_layer_id"], root_id)
        await self.issue("1", self.warehouse_b)
        frozen_movements = await self.movements()
        revision = await self.invoke("revise_cost", self.cost_request(root_id))
        self.assertEqual(await self.movements(), frozen_movements, "Historical movements cannot be rewritten")
        self.assertEqual(D(revision["net_consumption_after"]["quantity"]), D("5"))
        self.assertEqual(D(revision["net_consumption_after"]["total_cost"]), D("37.5"))
        self.assertTrue(revision["net_consumption_after"]["value_complete"])
        self.assertEqual(sum(D(item["total_value"]) for item in revision["stock_after"].values()), D("37.5"))
        async with self.sessions() as db:
            allocations = list((await db.scalars(select(FifoAllocation))).all())
            self.assertTrue(all(row.unit_cost_at_issue is None and row.total_cost_at_issue is None for row in allocations))
            self.assertTrue(all(row.unit_cost_current == D("7.5") for row in allocations))
            lines = list((await db.scalars(select(FifoReturnLine))).all())
            self.assertTrue(all(row.unit_cost_at_return is None for row in lines))
            layers = list((await db.scalars(select(FifoLayer))).all())
            self.assertTrue(all(row.root_cost_layer_id == root_id and row.unit_cost == D("7.5") for row in layers))
            balances = {row.warehouse_id: row for row in (await db.scalars(select(StockBalance))).all()}
            self.assertEqual(balances[self.warehouse_a].last_purchase_price, D("7.5"))
            self.assertEqual(balances[self.warehouse_a].last_purchase_at, NOW)
            self.assertIsNone(balances[self.warehouse_b].last_purchase_price)

    async def test_older_cost_revision_does_not_overwrite_latest_purchase_price(self):
        earlier, _ = await self.receive("2", "5")
        await self.receive("2", "9")
        result = await self.invoke("revise_cost", self.cost_request(earlier, "7"))
        self.assertFalse(result["last_purchase_price_updated"])
        async with self.sessions() as db:
            balance = await db.scalar(select(StockBalance).where(StockBalance.product_id == self.product_id))
            self.assertEqual(balance.last_purchase_price, D("9"))

    async def test_returns_reverse_latest_allocation_and_do_not_reopen_issued_order(self):
        first, _ = await self.receive("2", "10")
        second, _ = await self.receive("3", "20")
        issue_id = await self.issue("4")
        returned = await self.invoke("receive_return", self.return_request(issue_id, "3"))
        self.assertEqual([(row["root_layer_id"], D(row["quantity"])) for row in returned["lines"]],
                         [(second, D("2")), (first, D("1"))])
        self.assertEqual(D(returned["net_consumption"]["total_cost"]), D("10"))
        async with self.sessions() as db:
            balance = await db.scalar(select(StockBalance).where(StockBalance.product_id == self.product_id))
            self.assertEqual(balance.qty_on_hand, D("4"))
            self.assertEqual(balance.qty_quarantined, D("3"))
            self.assertEqual(balance.qty_available, D("1"))
            self.assertEqual(await db.scalar(select(func.count()).select_from(FifoReturn)), 1)

    async def test_same_request_is_idempotent_and_conflicting_replay_rejected(self):
        await self.receive("3", "5")
        issue_id = await self.issue("3")
        payload = self.return_request(issue_id, "1")
        results = await asyncio.wait_for(asyncio.gather(self.invoke("receive_return", payload),
                                                       self.invoke("receive_return", payload)), timeout=15)
        self.assertEqual(results[0], results[1])
        conflict = self.return_request(issue_id, "2", request_id=payload.request_id)
        with self.assertRaisesRegex(service.FifoReturnError, "request_id_conflict"):
            await self.invoke("receive_return", conflict)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(FifoReturn)), 1)

    async def test_parallel_returns_cannot_exceed_issued_quantity(self):
        await self.receive("3", "5")
        issue_id = await self.issue("3")
        results = await asyncio.wait_for(asyncio.gather(
            self.invoke("receive_return", self.return_request(issue_id, "2")),
            self.invoke("receive_return", self.return_request(issue_id, "2")), return_exceptions=True), timeout=15)
        self.assertEqual(sum(isinstance(item, dict) for item in results), 1)
        self.assertEqual(sum(isinstance(item, service.FifoReturnError) for item in results), 1)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.sum(FifoAllocation.returned_quantity))), D("2"))

    async def test_whole_issue_from_fractional_layers_returns_without_mutations_are_blocked(self):
        await self.receive("0.5", "0.3333")
        await self.receive("0.5", "0.3333")
        issue_id = await self.issue("1")
        async def snapshot():
            async with self.sessions() as db:
                return {table: (await db.execute(text(f"SELECT * FROM {table} ORDER BY id"))).mappings().all()
                        for table in ("stock_balances", "stock_movements", "fifo_layers", "fifo_allocations",
                                      "fifo_returns", "fifo_return_lines", "fifo_releases")}
        before = await snapshot()
        with self.assertRaisesRegex(service.FifoReturnError, "fractional_allocation_unsupported"):
            await self.invoke("receive_return", self.return_request(issue_id, "1"))
        self.assertEqual(await snapshot(), before)

    async def test_damaged_return_never_becomes_offerable(self):
        await self.receive("2", "5")
        issue_id = await self.issue("2")
        returned = await self.invoke("receive_return", self.return_request(issue_id, "1", condition="damaged"))
        before = await self.movements()
        with self.assertRaisesRegex(service.FifoReturnError, "condition_not_good"):
            await self.invoke("release_quarantine", self.release_request(returned["lines"][0]["layer_id"]))
        self.assertEqual(await self.movements(), before)
        async with self.sessions() as db:
            balance = await db.scalar(select(StockBalance).where(StockBalance.product_id == self.product_id))
            self.assertEqual(balance.qty_available, D("0"))
            self.assertEqual(await db.scalar(select(func.count()).select_from(FifoRelease)), 0)

    async def test_cost_revision_conflict_idempotence_provisional_and_unknown(self):
        root_id, _ = await self.receive("4")
        await self.issue("2")
        request = self.cost_request(root_id, "3", "provisional")
        result = await self.invoke("revise_cost", request)
        self.assertFalse(result["net_consumption_after"]["value_complete"])
        self.assertEqual(D(result["net_consumption_after"]["total_cost"]), D("6"))
        self.assertEqual(await self.invoke("revise_cost", request), result)
        with self.assertRaisesRegex(service.FifoReturnError, "revision_conflict"):
            await self.invoke("revise_cost", self.cost_request(root_id))
        cleared = await self.invoke("revise_cost", self.cost_request(root_id, None, "unknown", revision=1))
        self.assertIsNone(cleared["net_consumption_after"]["total_cost"])
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(FifoCostRevision)), 2)
            balance = await db.scalar(select(StockBalance).where(StockBalance.product_id == self.product_id))
            self.assertIsNone(balance.avg_cost)
            self.assertIsNone(balance.total_value)

    async def test_publication_hold_blocks_returns_cost_changes_and_releases(self):
        root_id, _ = await self.receive("3", "5")
        issue_id = await self.issue("3")
        returned = await self.invoke("receive_return", self.return_request(issue_id, "1"))
        async with self.sessions() as db:
            db.add(StockPublicationHold(id=str(uuid4()), shop_id=self.shop_id, warehouse_id=self.warehouse_a,
                                        active=True, assertions={"synthetic": True}))
            await db.commit()
        before = await self.movements()
        for action, payload in [
            ("receive_return", self.return_request(issue_id, "1")),
            ("release_quarantine", self.release_request(returned["lines"][0]["layer_id"])),
            ("revise_cost", self.cost_request(root_id)),
        ]:
            with self.subTest(action=action), self.assertRaises(StockPublicationHoldError):
                await self.invoke(action, payload)
        self.assertEqual(await self.movements(), before)

    async def test_historical_issue_without_fifo_cannot_fabricate_return_layers(self):
        async with self.sessions() as db:
            movement = StockMovement(idempotency_key=uuid4().hex, product_id=self.product_id,
                warehouse_id=self.warehouse_a, movement_type=MovementType.SALE_OUT, quantity=D("-1"),
                unit_cost=D("5"), balance_after=D("0"), avg_cost_after=D("0"))
            db.add(movement)
            await db.commit()
            issue_id = movement.id
        with self.assertRaisesRegex(service.FifoReturnError, "legacy_issue_unsupported"):
            await self.invoke("return_options", issue_id)
        with self.assertRaisesRegex(service.FifoReturnError, "legacy_issue_unsupported"):
            await self.invoke("receive_return", self.return_request(issue_id, "1"))
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(FifoLayer)), 0)

    async def test_stock_overview_unknown_provisional_and_quarantine_are_explicit(self):
        await self.receive("3", "5")
        issued = await self.issue("2")
        await self.invoke("receive_return", self.return_request(issued, "1", condition="damaged"))
        unknown_root, _ = await self.receive("2")
        async with self.sessions() as db:
            summary = await stock.stock_summary(db)
            items = await stock.stock_items(db)
            self.assertIsNone(summary["inventory_value"])
            self.assertEqual(summary["known_inventory_value"], 10)
            self.assertEqual(summary["unknown_quantity"], 2)
            self.assertEqual(summary["quarantined_total"], 1)
            self.assertFalse(summary["value_complete"])
            self.assertEqual(items[0]["available"], 3)
            self.assertIsNone(items[0]["total_value"])
            database_available = await db.scalar(text("SELECT qty_available FROM stock_balances WHERE product_id=:product"),
                                                   {"product": self.product_id})
            self.assertEqual(database_available, D("3"), "Generated SQL availability excludes quarantine too")
        await self.invoke("revise_cost", self.cost_request(unknown_root, "3", "provisional"))
        async with self.sessions() as db:
            summary = await stock.stock_summary(db)
            self.assertIsNone(summary["inventory_value"])
            self.assertEqual(summary["known_inventory_value"], 10)
            self.assertEqual(summary["provisional_inventory_value"], 6)
            self.assertEqual(summary["provisional_quantity"], 2)
            self.assertEqual(summary["unknown_quantity"], 0)
        await self.invoke("revise_cost", self.cost_request(unknown_root, "3", "known", revision=1))
        async with self.sessions() as db:
            summary = await stock.stock_summary(db)
            self.assertEqual(summary["inventory_value"], 16)
            self.assertEqual(summary["known_inventory_value"], 16)
            self.assertTrue(summary["value_complete"])

    async def test_stock_overview_rounds_each_layer_like_fifo_balance(self):
        await self.receive("0.003", "0.05")
        async with self.sessions() as db:
            summary = await stock.stock_summary(db)
            self.assertEqual(summary["inventory_value"], 0.0002)
            self.assertEqual(summary["known_inventory_value"], 0.0002)
            self.assertTrue(summary["value_complete"])
