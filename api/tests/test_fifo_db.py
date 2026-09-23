"""FIFO valuation and cutover invariants in isolated localhost PostgreSQL."""
import asyncio
import os
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.db_models import MovementType, Product, ReceivingStatus, Shop, Supplier, Warehouse
from inventory_hub.db_models_ext import ReceivingLine, ReceivingSession, Reservation, ShopOrder, ShopOrderItem, StockBalance, StockMovement
from inventory_hub.fifo_models import FifoAllocation, FifoCutover, FifoLayer, FifoState
from inventory_hub.fifo_types import FifoCutoverApply, FifoCutoverLayer, FifoCutoverPreview, FifoReceiptPreview, FifoReceiptApply
from inventory_hub.opening_stock_types import OpeningFinalizeRequest, OpeningPreviewRequest
from inventory_hub.stock_publication_models import StockPublicationHold
from inventory_hub.routers import receiving_db as receiving
from inventory_hub.services import opening_stock as opening
from inventory_hub.services import order_stock_ledger as ledger
from inventory_hub.services import fifo


TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")
NOW = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
D = Decimal


@unittest.skipUnless(TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class FifoDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("FIFO tests require a dedicated localhost *_catalog_test database")
        self.schema = "fifo_test_" + uuid4().hex
        sql_root = Path(__file__).resolve().parents[2] / "infra" / "db-init"
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            await connection.execute((sql_root / "001_schema.sql").read_text())
            await connection.execute("CREATE TYPE payment_status AS ENUM ('unpaid', 'partial', 'paid')")
            for filename in ("002_invoice_management.sql", "006_opening_stock.sql", "007_order_stock.sql",
                             "008_order_collection.sql", "009_stock_automation.sql", "010_stock_publication.sql"):
                await connection.execute((sql_root / filename).read_text())
            migrations = list(sql_root.glob("011_*.sql"))
            self.assertEqual(len(migrations), 1, "Exactly one FIFO migration is expected")
            await connection.execute(migrations[0].read_text())
        finally:
            await connection.close()
        self.engine = create_async_engine(TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1),
            poolclass=NullPool, connect_args={"server_settings": {"search_path": self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        self.patchers = [patch.object(opening, "now", return_value=NOW),
                         patch.object(fifo, "now", return_value=NOW),
                         patch.object(receiving, "_product_code_prefix", return_value="FIFO-"),
                         patch.object(receiving, "_sync_finalized_invoice", return_value=None)]
        for patcher in self.patchers:
            patcher.start()
        async with self.transaction() as db:
            warehouse = Warehouse(code="fifo-central", name="FIFO central")
            supplier = Supplier(code="fifo-receipt", name="FIFO receipt supplier")
            products = [Product(sku=sku, name=sku) for sku in ("SKU-A", "SKU-B", "LEGACY", "UNOPENED")]
            db.add_all([warehouse, supplier, *products])
            await db.flush()
            self.warehouse_id, self.supplier_id = warehouse.id, supplier.id
            self.products = {product.sku: product.id for product in products}
            self.shop_id = await db.scalar(select(Shop.id).where(Shop.code == "biketrek"))

    async def asyncTearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        await self.engine.dispose()
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            await connection.close()

    @asynccontextmanager
    async def transaction(self):
        async with self.sessions() as db:
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def receive(self, sku="SKU-A", quantity="1", cost="80"):
        async with self.transaction() as db:
            receipt = ReceivingSession(supplier_id=self.supplier_id, warehouse_id=self.warehouse_id,
                invoice_number="FIFO-" + uuid4().hex, status=ReceivingStatus.in_progress, total_lines=1, session_data={})
            db.add(receipt)
            await db.flush()
            identifier = receipt.id
            db.add(ReceivingLine(session_id=identifier, line_number=1, product_id=self.products[sku],
                ordered_qty=D(quantity), received_qty=D(quantity), unit_price=D(cost), status="matched"))
        await self.finalize_receipt(identifier)
        return identifier

    async def finalize_receipt(self, identifier):
        async with self.transaction() as db:
            return await receiving.finalize_session("fifo-receipt", identifier, receiving.FinalizeRequest(), db)

    async def open_stock(self, sku="SKU-A", quantity="1", cost="80"):
        async with self.sessions() as db:
            prepared = await opening.preview(db, OpeningPreviewRequest(request_id=uuid4(), warehouse_code="fifo-central",
                source_reference="Documented initial count", operator_name="Test operator", counted_at=NOW,
                csv_text=f"sku;quantity;unit_cost;unit\n{sku};{quantity};{cost};ks\n"))
        self.assertTrue(prepared["ready"], prepared)
        batch = prepared["batch"]
        async with self.transaction() as db:
            return await opening.finalize(db, batch["id"], OpeningFinalizeRequest(preview_hash=batch["preview_hash"],
                confirmed=True, receipts_reconciled=True))

    def line(self, sku="SKU-A", quantity="1", line_key=None):
        return {"line_key": line_key or str(uuid4()), "code": sku, "title": sku, "ean": "", "quantity": quantity,
            "unit": "ks", "kind": "product", "parent_uuid": "", "length": "", "length_unit": "",
            "has_native_identity": False, "identity_invalid": False, "classification": "identified",
            "product_id": self.products[sku], "sku": sku, "matched_by": "shared_sku", "reasons": []}

    def order(self, *lines):
        return {"uuid": str(uuid4()), "order_number": "FIFO-" + uuid4().hex,
            "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(), "origin": "eshop",
            "status_id": 1, "paid": False, "resolved": False, "lines": list(lines) or [self.line()]}

    async def apply_order(self, observed, action="issue"):
        async with self.transaction() as db:
            shop = await db.get(Shop, self.shop_id)
            order = await ledger.get_or_create_order(db, shop, observed, self.warehouse_id)
            plan = await ledger.plan_order(db, order, observed, action, self.warehouse_id, lock=True)
            self.assertTrue(plan["ready"], plan)
            return await ledger.apply_order(db, order, observed, action, self.warehouse_id, plan)

    async def seed_legacy(self, sku="LEGACY", quantity="2", reserved="0", cost="85"):
        async with self.transaction() as db:
            db.add(StockBalance(product_id=self.products[sku], warehouse_id=self.warehouse_id,
                qty_on_hand=D(quantity), qty_reserved=D(reserved), avg_cost=D(cost), total_value=D(quantity) * D(cost)))
            db.add(StockMovement(idempotency_key=uuid4().hex, product_id=self.products[sku], warehouse_id=self.warehouse_id,
                movement_type=MovementType.INITIAL, quantity=D(quantity), unit_cost=D(cost), balance_after=D(quantity),
                avg_cost_after=D(cost)))

    async def physical_snapshot(self):
        async with self.sessions() as db:
            return {table: (await db.execute(text(f"SELECT * FROM {table} ORDER BY id"))).mappings().all()
                    for table in ("stock_balances", "stock_movements", "reservations", "shop_order_items", "shop_sync_outbox")}

    async def valuation(self, sku="SKU-A"):
        async with self.sessions() as db:
            condition = (StockBalance.product_id == self.products[sku]) & (StockBalance.warehouse_id == self.warehouse_id)
            balance = await db.scalar(select(StockBalance).where(condition))
            state = await db.get(FifoState, (self.products[sku], self.warehouse_id))
            layers = (await db.scalars(select(FifoLayer).where(FifoLayer.product_id == self.products[sku],
                FifoLayer.warehouse_id == self.warehouse_id).order_by(FifoLayer.physical_received_at, FifoLayer.id))).all()
            allocations = (await db.scalars(select(FifoAllocation).join(FifoLayer, FifoLayer.id == FifoAllocation.layer_id)
                .where(FifoLayer.product_id == self.products[sku]).order_by(FifoAllocation.id))).all()
            issues = (await db.scalars(select(StockMovement).where(StockMovement.product_id == self.products[sku],
                StockMovement.warehouse_id == self.warehouse_id, StockMovement.movement_type == MovementType.SALE_OUT)
                .order_by(StockMovement.id))).all()
            return balance, state, layers, allocations, issues

    def documented_layer(self, quantity="1", cost="80", *, status="known", days_ago=2, reference="Receipt document A"):
        return FifoCutoverLayer(quantity=quantity, unit_cost=cost, cost_status=status,
            physical_received_at=NOW - timedelta(days=days_ago), source_reference=reference)

    async def cutover_preview(self, layers, sku="LEGACY", request_id=None):
        identifier = request_id or uuid4()
        async with self.transaction() as db:
            await fifo.cutover_preview(db, FifoCutoverPreview(request_id=identifier, sku=sku,
                warehouse_code="fifo-central", source_reference="Verified FIFO inventory sheet",
                operator_name="Test operator", counted_at=NOW, layers=layers))
        async with self.sessions() as db:
            return await db.get(FifoCutover, str(identifier))

    async def cutover_apply(self, batch):
        async with self.transaction() as db:
            return await fifo.cutover_apply(db, batch.id, FifoCutoverApply(preview_hash=batch.preview_hash,
                confirmed=True, quantities_verified=True, costs_documented=True))

    async def test_first_receipt_activates_fifo_and_replay_does_not_duplicate_layer(self):
        receipt = await self.receive(quantity="2", cost="80")
        balance, state, layers, allocations, issues = await self.valuation()
        self.assertIsNotNone(state)
        self.assertEqual((balance.qty_on_hand, balance.total_value), (D("2"), D("160")))
        self.assertEqual(len(layers), 1)
        self.assertEqual((layers[0].quantity_original, layers[0].quantity_remaining, layers[0].unit_cost),
                         (D("2"), D("2"), D("80")))
        self.assertIsNotNone(layers[0].receipt_movement_id)
        self.assertEqual((allocations, issues), ([], []))
        before = await self.physical_snapshot()
        await self.finalize_receipt(receipt)
        self.assertEqual(await self.physical_snapshot(), before)
        self.assertEqual([layer.id for layer in (await self.valuation())[2]], [layers[0].id])

    async def test_genuine_opening_stock_activates_fifo_with_documented_acquisition_cost(self):
        await self.open_stock(quantity="3", cost="80")
        balance, state, layers, allocations, _ = await self.valuation()
        self.assertIsNotNone(state)
        self.assertEqual((balance.qty_on_hand, balance.total_value), (D("3"), D("240")))
        self.assertEqual(len(layers), 1)
        self.assertEqual((layers[0].quantity_original, layers[0].quantity_remaining, layers[0].unit_cost),
                         (D("3"), D("3"), D("80")))
        self.assertIsNotNone(layers[0].receipt_movement_id)
        self.assertEqual(allocations, [])

    async def test_receipts_80_then_90_issue_first_80_and_leave_90_in_stock(self):
        await self.receive(cost="80")
        await self.receive(cost="90")
        await self.apply_order(self.order())
        balance, _, layers, allocations, issues = await self.valuation()
        self.assertEqual((balance.qty_on_hand, balance.total_value, balance.avg_cost), (D("1"), D("90"), D("90")))
        self.assertEqual([layer.quantity_remaining for layer in layers], [D("0"), D("1")])
        self.assertEqual(len(issues), 1)
        self.assertEqual((issues[0].unit_cost, issues[0].total_cost), (D("80"), D("80")))
        self.assertEqual(len(allocations), 1)
        self.assertEqual((allocations[0].layer_id, allocations[0].quantity, allocations[0].unit_cost_at_issue,
                          allocations[0].total_cost_at_issue), (layers[0].id, D("1"), D("80"), D("80")))

    async def test_single_issue_spanning_layers_records_exact_cost_per_allocation(self):
        await self.receive(quantity="2", cost="80")
        await self.receive(quantity="2", cost="90")
        await self.apply_order(self.order(self.line(quantity="3")))
        balance, _, layers, allocations, issues = await self.valuation()
        self.assertEqual((balance.qty_on_hand, balance.total_value), (D("1"), D("90")))
        self.assertEqual([layer.quantity_remaining for layer in layers], [D("0"), D("1")])
        self.assertEqual([(row.quantity, row.unit_cost_at_issue, row.total_cost_at_issue) for row in allocations],
                         [(D("2"), D("80"), D("160")), (D("1"), D("90"), D("90"))])
        self.assertEqual(issues[0].total_cost, D("250"))

    async def test_duplicate_sku_lines_consume_in_stable_line_order_not_source_array_order(self):
        await self.receive(cost="80")
        await self.receive(cost="90")
        first = "00000000-0000-0000-0000-000000000001"
        second = "00000000-0000-0000-0000-000000000002"
        await self.apply_order(self.order(self.line(line_key=second), self.line(line_key=first)))
        async with self.sessions() as db:
            costs = (await db.execute(select(ShopOrderItem.external_item_id, FifoAllocation.unit_cost_at_issue)
                .join(Reservation, Reservation.shop_order_item_id == ShopOrderItem.id)
                .join(FifoAllocation, FifoAllocation.issue_movement_id == Reservation.sale_movement_id)
                .order_by(ShopOrderItem.external_item_id))).all()
        self.assertEqual(costs, [(first, D("80")), (second, D("90"))])
        balance, _, layers, allocations, issues = await self.valuation()
        self.assertEqual((balance.qty_on_hand, balance.total_value), (D("0"), D("0")))
        self.assertEqual(sum(allocation.total_cost_at_issue for allocation in allocations), D("170"))
        self.assertEqual(sum(movement.total_cost for movement in issues), D("170"))
        self.assertTrue(all(layer.quantity_remaining == 0 for layer in layers))

    async def test_reservation_and_cancellation_do_not_consume_or_revalue_fifo_layers(self):
        await self.receive(quantity="2", cost="80")
        _, initial, layers, _, _ = await self.valuation()
        layer_facts = [(layer.id, layer.quantity_remaining, layer.unit_cost) for layer in layers]
        observed = self.order(self.line(quantity="2"))
        await self.apply_order(observed, "reserve")
        reserved, state, layers, allocations, issues = await self.valuation()
        self.assertEqual((reserved.qty_reserved, reserved.total_value), (D("2"), D("160")))
        self.assertEqual([(layer.id, layer.quantity_remaining, layer.unit_cost) for layer in layers], layer_facts)
        self.assertEqual(state.revision, initial.revision)
        self.assertEqual((allocations, issues), ([], []))
        await self.apply_order(observed, "cancel")
        cancelled, state, layers, allocations, issues = await self.valuation()
        self.assertEqual((cancelled.qty_reserved, cancelled.total_value), (D("0"), D("160")))
        self.assertEqual([(layer.id, layer.quantity_remaining, layer.unit_cost) for layer in layers], layer_facts)
        self.assertEqual((allocations, issues), ([], []))

    async def test_legacy_balance_remains_legacy_until_documented_cutover(self):
        await self.seed_legacy(quantity="2", cost="85")
        await self.receive(sku="LEGACY", quantity="1", cost="100")
        _, state, layers, allocations, _ = await self.valuation("LEGACY")
        self.assertIsNone(state)
        self.assertEqual((layers, allocations), ([], []))
        await self.apply_order(self.order(self.line(sku="LEGACY")))
        balance, state, layers, allocations, issues = await self.valuation("LEGACY")
        self.assertIsNone(state)
        self.assertEqual((layers, allocations), ([], []))
        self.assertEqual((balance.qty_on_hand, balance.avg_cost, issues[0].total_cost), (D("2"), D("90"), D("90")))
        async with self.sessions() as db:
            view = await fifo.stock(db, self.products["LEGACY"], "fifo-central")
        self.assertEqual(view["valuation"]["mode"], "legacy")

    async def test_documented_cutover_preserves_physical_quantity_and_historical_movements(self):
        await self.seed_legacy(quantity="2", reserved="1", cost="999")
        before = await self.physical_snapshot()
        batch = await self.cutover_preview([self.documented_layer(cost="90", days_ago=1, reference="Receipt document B"),
            self.documented_layer(cost="80", days_ago=3)])
        self.assertEqual(await self.physical_snapshot(), before, "Preparing a cost cutover has no ledger effect")
        result = await self.cutover_apply(batch)
        balance, state, layers, allocations, _ = await self.valuation("LEGACY")
        self.assertEqual((balance.qty_on_hand, balance.qty_reserved), (D("2"), D("1")))
        self.assertEqual((balance.avg_cost, balance.total_value), (D("85"), D("170")))
        self.assertEqual(state.cutover_id, batch.id)
        self.assertEqual([layer.unit_cost for layer in layers], [D("80"), D("90")])
        self.assertEqual([layer.cutover_id for layer in layers], [batch.id, batch.id])
        self.assertTrue(all(layer.provenance for layer in layers))
        after = await self.physical_snapshot()
        for table in ("stock_movements", "reservations", "shop_order_items", "shop_sync_outbox"):
            self.assertEqual(after[table], before[table])
        self.assertEqual(allocations, [])
        self.assertEqual(await self.cutover_apply(batch), result)
        self.assertEqual([layer.id for layer in (await self.valuation("LEGACY"))[2]], [layer.id for layer in layers])

    async def test_unknown_documented_cost_allows_physical_issue_without_fabricating_zero_cost(self):
        await self.seed_legacy(quantity="2", cost="85")
        batch = await self.cutover_preview([self.documented_layer(quantity="2", cost=None, status="unknown")])
        await self.cutover_apply(batch)
        balance, _, _, _, _ = await self.valuation("LEGACY")
        self.assertIsNone(balance.avg_cost)
        self.assertIsNone(balance.total_value)
        await self.apply_order(self.order(self.line(sku="LEGACY")))
        balance, _, layers, allocations, issues = await self.valuation("LEGACY")
        self.assertEqual((balance.qty_on_hand, layers[0].quantity_remaining), (D("1"), D("1")))
        self.assertIsNone(balance.total_value)
        self.assertEqual((issues[0].unit_cost, issues[0].total_cost), (None, None))
        self.assertEqual((allocations[0].unit_cost_at_issue, allocations[0].total_cost_at_issue,
                          allocations[0].cost_status_at_issue), (None, None, "unknown"))
        async with self.sessions() as db:
            view = await fifo.stock(db, self.products["LEGACY"], "fifo-central")
        self.assertFalse(view["valuation"]["value_complete"])

    async def test_unknown_later_layer_does_not_erase_known_cost_of_earlier_issue(self):
        await self.seed_legacy(quantity="2")
        batch = await self.cutover_preview([self.documented_layer(cost="80", days_ago=3),
            self.documented_layer(cost=None, status="unknown", days_ago=1)])
        await self.cutover_apply(batch)
        await self.apply_order(self.order(self.line(sku="LEGACY")))
        balance, _, layers, allocations, issues = await self.valuation("LEGACY")
        self.assertEqual((issues[0].unit_cost, issues[0].total_cost), (D("80"), D("80")))
        self.assertEqual(allocations[0].cost_status_at_issue, "known")
        self.assertIsNone(balance.total_value)
        self.assertEqual([layer.quantity_remaining for layer in layers], [D("0"), D("1")])

    async def test_reservation_changed_after_cutover_preview_invalidates_confirmation(self):
        await self.seed_legacy(quantity="2")
        batch = await self.cutover_preview([self.documented_layer(quantity="2")])
        async with self.transaction() as db:
            await db.execute(update(StockBalance).where(StockBalance.product_id == self.products["LEGACY"])
                .values(qty_reserved=D("1")))
        before = await self.physical_snapshot()
        with self.assertRaises(fifo.FifoError):
            await self.cutover_apply(batch)
        self.assertEqual(await self.physical_snapshot(), before)
        self.assertIsNone((await self.valuation("LEGACY"))[1])

    async def test_receipt_after_cutover_preview_invalidates_counted_quantity_snapshot(self):
        await self.seed_legacy(quantity="2")
        batch = await self.cutover_preview([self.documented_layer(quantity="2")])
        await self.receive(sku="LEGACY", quantity="1", cost="90")
        before = await self.physical_snapshot()
        with self.assertRaises(fifo.FifoError):
            await self.cutover_apply(batch)
        self.assertEqual(await self.physical_snapshot(), before)
        self.assertIsNone((await self.valuation("LEGACY"))[1])

    async def test_concurrent_cutover_confirmation_creates_one_layer_set_and_one_activation(self):
        await self.seed_legacy(quantity="2")
        batch = await self.cutover_preview([self.documented_layer(quantity="2")])
        first, second = await asyncio.wait_for(asyncio.gather(self.cutover_apply(batch), self.cutover_apply(batch)), timeout=5)
        self.assertEqual(first, second)
        balance, state, layers, allocations, _ = await self.valuation("LEGACY")
        self.assertEqual((state.cutover_id, len(layers), balance.qty_on_hand), (batch.id, 1, D("2")))
        self.assertEqual(allocations, [])

    async def test_hold_blocks_cutover_apply_without_layer_or_valuation_change(self):
        await self.seed_legacy(quantity="2")
        batch = await self.cutover_preview([self.documented_layer(quantity="2")])
        async with self.transaction() as db:
            db.add(StockPublicationHold(id=str(uuid4()), shop_id=self.shop_id, warehouse_id=self.warehouse_id,
                active=True, assertions={"external_writers_paused": True, "orders_reconciled": True}, created_at=NOW))
        before = await self.physical_snapshot()
        with self.assertRaises(fifo.FifoError) as raised:
            await self.cutover_apply(batch)
        self.assertEqual(raised.exception.code, "stock_publication_warehouse_held")
        self.assertEqual(await self.physical_snapshot(), before)
        self.assertIsNone((await self.valuation("LEGACY"))[1])

    async def test_unknown_documented_receipt_issues_without_invoice_and_replays_once(self):
        payload = FifoReceiptPreview(request_id=uuid4(), sku="SKU-A", warehouse_code="fifo-central", quantity="2",
            unit_cost=None, cost_status="unknown", physical_received_at=NOW-timedelta(hours=1),
            source_reference="Delivery note without invoice", operator_name="Test operator")
        before = await self.physical_snapshot()
        async with self.transaction() as db:
            prepared = await fifo.receipt_preview(db, payload)
        self.assertEqual(await self.physical_snapshot(), before)
        confirmation = FifoReceiptApply(preview_hash=prepared["preview_hash"], confirmed=True,
            quantities_verified=True, costs_documented=True)
        async with self.transaction() as db:
            result = await fifo.receipt_apply(db, prepared["id"], confirmation)
        async with self.transaction() as db:
            replay = await fifo.receipt_apply(db, prepared["id"], confirmation)
        self.assertEqual(result, replay)
        balance, state, layers, _, _ = await self.valuation()
        self.assertEqual((len(layers), balance.qty_on_hand, layers[0].cost_status), (1, D("2"), "unknown"))
        self.assertIsNone(balance.avg_cost)
        self.assertIsNone(balance.total_value)
        self.assertIsNone(balance.last_purchase_price)
        self.assertIsNotNone(balance.last_purchase_at)
        await self.apply_order(self.order(self.line(quantity="1")))
        balance, _, _, allocations, issues = await self.valuation()
        self.assertEqual(balance.qty_on_hand, D("1"))
        self.assertEqual(len(allocations), 1)
        self.assertIsNone(issues[0].total_cost)
        self.assertIsNone(issues[0].unit_cost)
        self.assertIsNone(issues[0].avg_cost_after)

    async def test_provisional_receipt_is_explicit_estimate_and_legacy_unknown_is_blocked(self):
        payload = FifoReceiptPreview(request_id=uuid4(), sku="SKU-A", warehouse_code="fifo-central", quantity="2",
            unit_cost="80", cost_status="provisional", physical_received_at=NOW-timedelta(hours=1),
            source_reference="Documented provisional supplier cost", operator_name="Test operator")
        async with self.transaction() as db:
            prepared = await fifo.receipt_preview(db, payload)
        async with self.transaction() as db:
            result = await fifo.receipt_apply(db, prepared["id"], FifoReceiptApply(preview_hash=prepared["preview_hash"],
                confirmed=True, quantities_verified=True, costs_documented=True))
        self.assertFalse(result["valuation"]["value_complete"])
        self.assertEqual(D(result["valuation"]["provisional_value"]), D("160"))
        await self.seed_legacy()
        before = await self.physical_snapshot()
        with self.assertRaises(fifo.FifoError) as raised:
            async with self.transaction() as db:
                await fifo.receipt_preview(db, payload.model_copy(update={"request_id": uuid4(), "sku": "LEGACY",
                    "unit_cost": None, "cost_status": "unknown"}))
        self.assertEqual(raised.exception.code, "fifo_cutover_required")
        self.assertEqual(await self.physical_snapshot(), before)

    async def test_two_receipt_drafts_on_missing_balance_cannot_bypass_snapshot_after_insert_wait(self):
        prepared = []
        for reference in ("Delivery A", "Delivery B"):
            async with self.transaction() as db:
                prepared.append(await fifo.receipt_preview(db, FifoReceiptPreview(request_id=uuid4(), sku="SKU-A",
                    warehouse_code="fifo-central", quantity="1", unit_cost="80", cost_status="known",
                    physical_received_at=NOW-timedelta(hours=1), source_reference=reference, operator_name="Operator")))
        original = fifo._snapshot
        barrier = asyncio.Barrier(2)
        async def synchronized_snapshot(*args):
            result = await original(*args)
            await barrier.wait()
            return result
        async def apply(batch):
            async with self.transaction() as db:
                return await fifo.receipt_apply(db, batch["id"], FifoReceiptApply(preview_hash=batch["preview_hash"],
                    confirmed=True, quantities_verified=True, costs_documented=True))
        with patch.object(fifo, "_snapshot", side_effect=synchronized_snapshot):
            results = await asyncio.wait_for(asyncio.gather(*(apply(batch) for batch in prepared), return_exceptions=True), timeout=5)
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1, results)
        conflicts = [result for result in results if isinstance(result, fifo.FifoError)]
        self.assertEqual([result.code for result in conflicts], ["fifo_preview_stale"])
        balance, _, layers, _, _ = await self.valuation()
        self.assertEqual((balance.qty_on_hand, len(layers)), (D("1"), 1))
