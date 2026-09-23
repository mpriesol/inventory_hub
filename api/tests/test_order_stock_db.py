"""Order reservation and issue invariants in a disposable local PostgreSQL schema."""
import asyncio
import copy
import os
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.db_models import MovementType, Product, ProductGroup, ReceivingStatus, Shop, Supplier, Warehouse
from inventory_hub.db_models_ext import (
    ReceivingLine, ReceivingSession, Reservation, ShopOrder, ShopOrderItem, ShopProduct, StockBalance, StockMovement,
)
from inventory_hub.order_stock_models import OrderStockPolicy, OrderStockPreview
from inventory_hub.order_stock_types import OrderStockApplyRequest, OrderStockConfigureRequest, OrderStockPreviewRequest
from inventory_hub.routers import receiving_db as receiving
from inventory_hub.services import order_stock as service
from inventory_hub.services import order_stock_source as source
from test_order_stock import ACTIONS, NOW, STATUSES, configure_body, preview_body, raw_line, raw_order, raw_page


TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")
D = Decimal


@unittest.skipUnless(TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class OrderStockDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("Order-stock tests require a dedicated localhost *_catalog_test database")
        self.schema = "order_stock_test_" + uuid4().hex
        self.sql_root = Path(__file__).resolve().parents[2] / "infra" / "db-init"
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            await connection.execute((self.sql_root / "001_schema.sql").read_text())
            # Historical 002 checks enum names globally; create this schema's
            # own enum before applying it, as the receiving suite also does.
            await connection.execute("CREATE TYPE payment_status AS ENUM ('unpaid', 'partial', 'paid')")
            await connection.execute((self.sql_root / "002_invoice_management.sql").read_text())
            await connection.execute((self.sql_root / "007_order_stock.sql").read_text())
            await connection.execute((self.sql_root / "010_stock_publication.sql").read_text())
            await connection.execute((self.sql_root / "011_fifo.sql").read_text())
        finally:
            await connection.close()
        self.engine = create_async_engine(TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1),
            poolclass=NullPool, connect_args={"server_settings": {"search_path": self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        self.raw_orders, self.source_reads = {}, []
        self.statuses = copy.deepcopy(STATUSES)
        self.clock_patch = patch.object(service, "now", return_value=NOW)
        self.clock = self.clock_patch.start()
        self.transport_patch = patch.object(source.UpgatesClient, "from_shop", side_effect=self.client)
        self.transport_patch.start()
        self.invoice_patch = patch.object(receiving, "_update_invoice_status", return_value=True)
        self.invoice_patch.start()
        self.prefix_patch = patch.object(receiving, "_product_code_prefix", return_value="TEST-")
        self.prefix_patch.start()
        async with self.sessions() as db:
            warehouse = Warehouse(code="order-test", name="Order test warehouse")
            supplier = Supplier(code="order-receipt", name="Order receipt supplier")
            products = [Product(sku=sku, name=sku) for sku in ("SKU-A", "SKU-B", "SKU-C")]
            db.add_all([warehouse, supplier, *products])
            await db.commit()
            self.warehouse_id, self.supplier_id = warehouse.id, supplier.id
            self.products = {product.sku: product.id for product in products}
            self.shops = {shop.code: shop.id for shop in (await db.scalars(select(Shop))).all()}
        await self.configure()
        await self.configure(shop_code="xtrek")

    async def asyncTearDown(self):
        self.clock_patch.stop()
        self.transport_patch.stop()
        self.invoice_patch.stop()
        self.prefix_patch.stop()
        await self.engine.dispose()
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            await connection.close()

    def client(self, shop_code):
        def read_order(params):
            self.source_reads.append((shop_code, dict(params)))
            order = self.raw_orders.get((shop_code, params["order_numbers"]))
            return raw_page(copy.deepcopy(order)) if order else raw_page(), copy.deepcopy(self.statuses)
        # No write method exists on this synthetic upstream client.
        return SimpleNamespace(read_order_audit=read_order, read_order_statuses=lambda: copy.deepcopy(self.statuses),
                               session=SimpleNamespace(close=Mock()))

    def put(self, raw, shop="biketrek"):
        self.raw_orders[(shop, raw["order_number"])] = copy.deepcopy(raw)
        return raw

    async def configure(self, shop_code="biketrek", actions=None, **changes):
        async with self.sessions() as db:
            try:
                options = await service.options(db, shop_code)
                request = OrderStockConfigureRequest(**configure_body(shop_code=shop_code,
                    status_hash=options["status_hash"], status_actions=actions or dict(ACTIONS), **changes))
                return await service.configure(db, request)
            except Exception:
                await db.rollback()
                raise

    async def preview(self, raw=None, shop="biketrek", request_id=None, expect_ready=True):
        if raw is not None:
            self.put(raw, shop)
        else:
            raw = self.raw_orders[(shop, "TEST-01")]
        async with self.sessions() as db:
            try:
                response = await service.preview(db, OrderStockPreviewRequest(**preview_body(
                    request_id=request_id or str(uuid4()), shop_code=shop, order_number=raw["order_number"])))
                if expect_ready is not None:
                    self.assertEqual(response["ready"], expect_ready, response)
                return response["preview"]
            except Exception:
                await db.rollback()
                raise

    async def apply(self, preview, physical=None, **changes):
        payload = OrderStockApplyRequest(preview_hash=changes.pop("preview_hash", preview["preview_hash"]),
            confirmed=True, physical_confirmed=preview["action"] == "issue" if physical is None else physical, **changes)
        async with self.sessions() as db:
            try:
                return await service.apply(db, preview["id"], payload)
            except Exception:
                await db.rollback()
                raise

    async def process(self, raw, shop="biketrek"):
        return await self.apply(await self.preview(raw, shop))

    async def seed(self, sku, quantity="10", cost="10", total=None):
        async with self.sessions() as db:
            db.add(StockBalance(product_id=self.products[sku], warehouse_id=self.warehouse_id,
                qty_on_hand=D(quantity), avg_cost=D(cost), total_value=D(total) if total is not None else D(quantity) * D(cost)))
            await db.commit()

    async def balance(self, sku):
        async with self.sessions() as db:
            row = await db.scalar(select(StockBalance).where(StockBalance.product_id == self.products[sku],
                                                             StockBalance.warehouse_id == self.warehouse_id))
            return None if row is None else (row.qty_on_hand, row.qty_reserved, row.avg_cost, row.total_value)

    async def movements(self):
        async with self.sessions() as db:
            return (await db.scalars(select(StockMovement).order_by(StockMovement.id))).all()

    async def physical_snapshot(self):
        async with self.sessions() as db:
            return {table: (await db.execute(text(f"SELECT * FROM {table} ORDER BY id"))).mappings().all()
                    for table in ("products", "product_groups", "product_identifiers", "shop_products", "stock_balances", "stock_movements", "shop_sync_outbox")}

    async def holds(self, raw):
        async with self.sessions() as db:
            rows = (await db.execute(select(Reservation, ShopOrderItem).join(ShopOrderItem, Reservation.shop_order_item_id == ShopOrderItem.id)
                .join(ShopOrder, ShopOrderItem.order_id == ShopOrder.id).where(ShopOrder.stock_source_uuid == raw["uuid"]))).all()
            return [(item.external_item_id, reservation.product_id, reservation.quantity, reservation.shortage_qty, reservation.status.value)
                    for reservation, item in rows]

    async def test_preview_has_no_physical_effect_and_partial_reservation_keeps_backorder_balance_absent(self):
        await self.seed("SKU-A", "2")
        raw = raw_order(raw_line(quantity="5"), raw_line(code="SKU-B", quantity="3"))
        before = await self.physical_snapshot()
        preview = await self.preview(raw)
        self.assertEqual(await self.physical_snapshot(), before)
        async with self.sessions() as db:
            row = (await db.execute(select(ShopOrder).where(ShopOrder.stock_source_uuid == raw["uuid"]))).scalar_one()
            self.assertIsNone(row.currency, "Stock processing must not invent the order's commercial currency")
            self.assertTrue((await db.execute(select(OrderStockPreview.result.is_(None)).where(
                OrderStockPreview.id == preview["id"],
            ))).scalar_one(), "A prepared preview has no stored result, including no JSON null")
        result = await self.apply(preview)
        self.assertEqual(result["stock_state"], "reserved")
        async with self.sessions() as db:
            prices = (await db.execute(select(ShopOrderItem.unit_price, ShopOrderItem.total_price).where(
                ShopOrderItem.order_id == result["order_id"],
            ))).all()
            self.assertEqual(prices, [(None, None), (None, None)])
        self.assertEqual((await self.balance("SKU-A"))[:2], (D("2"), D("2")))
        self.assertIsNone(await self.balance("SKU-B"), "An unavailable supplier/backorder item must not fabricate a physical balance")
        rows = {product: (quantity, shortage) for _, product, quantity, shortage, _ in await self.holds(raw)}
        self.assertEqual(rows, {self.products["SKU-A"]: (D("5"), D("3")), self.products["SKU-B"]: (D("3"), D("3"))})
        self.assertEqual(await self.movements(), [])

    async def test_edit_remove_replace_multiple_same_sku_lines_and_cancel_preserve_other_order_holds(self):
        await self.seed("SKU-A", "10")
        await self.seed("SKU-B", "4")
        other = raw_order(raw_line(quantity="2"), order_number="OTHER-01")
        await self.process(other)
        first, second = raw_line(quantity="3"), raw_line(quantity="2")
        raw = raw_order(first, second)
        await self.process(raw)
        self.assertEqual((await self.balance("SKU-A"))[1], D("7"))
        raw["products"] = [{**first, "code": "SKU-B", "quantity": "1"}]
        await self.process(raw)
        self.assertEqual((await self.balance("SKU-A"))[1], D("2"))
        self.assertEqual((await self.balance("SKU-B"))[1], D("1"))
        raw["products"].append(raw_line(quantity="4"))
        await self.process(raw)
        self.assertEqual((await self.balance("SKU-A"))[1], D("6"))
        raw.update(status_id=2, products=[])
        await self.process(raw)
        self.assertEqual((await self.balance("SKU-A"))[:2], (D("10"), D("2")))
        self.assertEqual((await self.balance("SKU-B"))[:2], (D("4"), D("0")))
        self.assertEqual(await self.movements(), [], "Editing/cancellation releases reservations, not physical stock")
        self.assertEqual(sum(quantity - shortage for _, _, quantity, shortage, status in await self.holds(other)
                             if status in ("reserved", "backorder")), D("2"))

    async def test_issue_shortage_is_all_or_nothing_even_when_some_products_are_available(self):
        await self.seed("SKU-A", "4")
        raw = raw_order(raw_line(quantity="2"), raw_line(code="SKU-B", quantity="1"), status_id=8)
        before = await self.physical_snapshot()
        preview = await self.preview(raw, expect_ready=False)
        self.assertTrue(preview["plan"]["errors"])
        with self.assertRaises(service.OrderStockError):
            await self.apply(preview)
        self.assertEqual(await self.physical_snapshot(), before)

    async def test_received_status_cannot_be_configured_to_issue_and_paid_web_order_only_reserves(self):
        await self.seed("SKU-A", "2")
        async with self.sessions() as db:
            options = await service.options(db, "biketrek")
        self.assertNotIn("issue", options["allowed_actions"]["1"])
        with self.assertRaises(service.OrderStockError):
            await self.configure(actions={**ACTIONS, "1": "issue"})
        raw = raw_order(paid_date="2026-09-23", resolved_yn=True)
        preview = await self.preview(raw)
        self.assertEqual(preview["action"], "reserve")
        await self.apply(preview)
        self.assertEqual((await self.balance("SKU-A"))[:2], (D("2"), D("1")))
        self.assertEqual(await self.movements(), [])

    async def test_fractional_piece_balances_block_reserve_and_issue_but_cancel_releases_old_hold(self):
        await self.seed("SKU-A", "2")
        for on_hand, reserved in (("1.5", "0"), ("2", "0.5")):
            async with self.sessions() as db:
                await db.execute(update(StockBalance).where(StockBalance.product_id == self.products["SKU-A"])
                    .values(qty_on_hand=D(on_hand), qty_reserved=D(reserved), total_value=D(on_hand) * 10))
                await db.commit()
            before = await self.physical_snapshot()
            for status in (1, 8):
                with self.subTest(on_hand=on_hand, reserved=reserved, status=status):
                    raw = raw_order(status_id=status, order_number=f"FRACTION-{on_hand}-{status}")
                    preview = await self.preview(raw, expect_ready=False)
                    self.assertIn("order_stock_unit_unsupported", [error["code"] for error in preview["plan"]["errors"]])
                    with self.assertRaises(service.OrderStockError):
                        await self.apply(preview)
                    self.assertEqual(await self.physical_snapshot(), before)
        async with self.sessions() as db:
            await db.execute(update(StockBalance).where(StockBalance.product_id == self.products["SKU-A"])
                .values(qty_on_hand=D("2"), qty_reserved=D("0"), total_value=D("20")))
            await db.commit()
        raw = raw_order(order_number="OLD-FRACTIONAL-HOLD")
        await self.process(raw)
        async with self.sessions() as db:
            await db.execute(update(StockBalance).where(StockBalance.product_id == self.products["SKU-A"])
                .values(qty_on_hand=D("1.5"), qty_reserved=D("0.5"), total_value=D("15")))
            await db.execute(update(Reservation).values(shortage_qty=D("0.5")))
            await db.commit()
        raw.update(status_id=2, products=[])
        await self.process(raw)
        self.assertEqual((await self.balance("SKU-A"))[:2], (D("1.5"), D("0")))
        self.assertEqual(await self.movements(), [])

    async def test_issue_uses_own_reservation_but_cannot_consume_another_orders_allocation(self):
        await self.seed("SKU-A", "3")
        other = raw_order(raw_line(quantity="2"), order_number="OTHER-01")
        await self.process(other)
        raw = raw_order(raw_line(quantity="2"))
        await self.process(raw)
        raw["status_id"] = 8
        blocked = await self.preview(raw, expect_ready=False)
        with self.assertRaises(service.OrderStockError):
            await self.apply(blocked)
        raw["products"][0]["quantity"] = "1"
        result = await self.process(raw)
        self.assertEqual(result["stock_state"], "issued")
        self.assertEqual((await self.balance("SKU-A"))[:2], (D("2"), D("2")))
        self.assertEqual(len(await self.movements()), 1)

    async def test_decimal_cost_conservation_residual_and_zero_ending_balance(self):
        await self.seed("SKU-A", "3", cost="3.3333", total="10.0000")
        await self.process(raw_order(raw_line(quantity="1"), status_id=8))
        await self.process(raw_order(raw_line(quantity="2"), status_id=8, order_number="SECOND-ISSUE"))
        movements = await self.movements()
        self.assertEqual([movement.movement_type for movement in movements], [MovementType.SALE_OUT, MovementType.SALE_OUT])
        self.assertEqual([movement.quantity for movement in movements], [D("-1"), D("-2")])
        self.assertEqual(sum(movement.total_cost for movement in movements), D("10.0000"))
        self.assertTrue(all(movement.total_cost >= 0 for movement in movements))
        balance = await self.balance("SKU-A")
        self.assertEqual(balance[0], D("0"))
        self.assertEqual(balance[1], D("0"))
        self.assertEqual(balance[3], D("0"), "Last issue consumes the remaining cost, including rounding residue")

    async def test_inconsistent_acquisition_value_blocks_new_stock_actions_but_allows_release(self):
        await self.seed("SKU-A", "2", cost="10")
        raw = raw_order(order_number="COST-CHECK")
        await self.process(raw)
        async with self.sessions() as db:
            await db.execute(update(StockBalance).where(StockBalance.product_id == self.products["SKU-A"])
                             .values(avg_cost=D("0")))
            await db.commit()
        before = await self.physical_snapshot()
        for status in (1, 8):
            raw["status_id"] = status
            blocked = await self.preview(raw, expect_ready=False)
            self.assertIn("order_stock_invariant", [error["code"] for error in blocked["plan"]["errors"]])
            with self.assertRaises(service.OrderStockError):
                await self.apply(blocked)
            self.assertEqual(await self.physical_snapshot(), before)
        raw["status_id"] = 2
        await self.process(raw)
        self.assertEqual((await self.balance("SKU-A"))[:2], (D("2"), D("0")))
        self.assertEqual(await self.movements(), [])

    async def test_paid_completed_pos_with_shared_sku_issues_once_without_reparenting_canonical_product(self):
        await self.seed("SKU-A", "2")
        async with self.sessions() as db:
            group = ProductGroup(code="ACTUAL-WEB-PARENT", name="Actual web family")
            db.add(group)
            await db.flush()
            await db.execute(update(Product).where(Product.id == self.products["SKU-A"]).values(group_id=group.id))
            db.add(ShopProduct(shop_id=self.shops["xtrek"], product_id=self.products["SKU-A"], external_code=group.code,
                parent_code=group.code, variant_code="SKU-A", is_variant=True))
            await db.commit()
            group_id = group.id
        raw = raw_order(raw_line(title="xTrek", product_id=900, option_set_id=101), origin="cash-register",
                        paid_date="2026-09-23", resolved_yn=True)
        preview = await self.preview(raw)
        self.assertEqual(preview["action"], "issue")
        self.assertEqual(preview["source"]["lines"][0]["matched_by"], "shared_sku")
        result = await self.apply(preview)
        reads = len(self.source_reads)
        self.assertEqual(await self.apply(preview), result)
        self.assertEqual(len(self.source_reads), reads, "Replaying a completed preview reads its result without another upstream request")
        async with self.sessions() as db:
            self.assertEqual((await db.get(Product, self.products["SKU-A"])).group_id, group_id)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ShopProduct)), 1)
        self.assertEqual(len(await self.movements()), 1)

    async def test_manual_rows_are_excluded_and_code_less_identified_rows_block_stock(self):
        await self.seed("SKU-A", "2")
        raw = raw_order(raw_line(), raw_line(code="", title="Manually charged workshop part"), status_id=8)
        await self.process(raw)
        self.assertEqual((await self.balance("SKU-A"))[0], D("1"))
        self.assertEqual(len(await self.movements()), 1)
        ambiguous = raw_order(raw_line(code="", product_id=900), order_number="UNCODED", status_id=8)
        before = await self.physical_snapshot()
        try:
            preview = await self.preview(ambiguous, expect_ready=False)
            with self.assertRaises(service.OrderStockError):
                await self.apply(preview)
        except service.OrderStockError:
            pass
        self.assertEqual(await self.physical_snapshot(), before)

    async def test_manual_only_service_order_completes_without_creating_any_stock_rows(self):
        raw = raw_order(raw_line(code="", title="Workshop labour", quantity="1.5", unit="hod"), status_id=8)
        before = await self.physical_snapshot()
        preview = await self.preview(raw)
        self.assertEqual(preview["plan"]["lines"], [])
        self.assertEqual(len(preview["plan"]["excluded_lines"]), 1)
        result = await self.apply(preview)
        self.assertEqual(result["stock_state"], "issued")
        self.assertEqual(result["movements_created"], 0)
        self.assertEqual(await self.physical_snapshot(), before)
        self.assertEqual(await self.holds(raw), [])

    async def test_pre_cutover_and_legacy_unmanaged_orders_never_acquire_holds(self):
        await self.seed("SKU-A", "2")
        before = await self.physical_snapshot()
        old = raw_order(creation_time=(NOW - timedelta(seconds=1)).isoformat())
        with self.assertRaises(service.OrderStockError) as raised:
            await self.preview(old)
        self.assertEqual(raised.exception.code, "order_stock_before_cutover")
        raw = raw_order()
        async with self.sessions() as db:
            db.add(ShopOrder(shop_id=self.shops["biketrek"], external_id=raw["order_number"],
                             external_code=raw["order_number"], order_date=NOW))
            await db.commit()
        with self.assertRaises(service.OrderStockError):
            await self.preview(raw)
        self.assertEqual(await self.physical_snapshot(), before)

    async def test_apply_rechecks_fresh_source_and_rejects_missing_source_without_mutations(self):
        await self.seed("SKU-A", "3")
        raw = raw_order()
        preview = await self.preview(raw)
        before = await self.physical_snapshot()
        raw["products"][0]["quantity"] = "2"
        self.put(raw)
        with self.assertRaises(service.OrderStockError) as raised:
            await self.apply(preview)
        self.assertEqual(raised.exception.code, "order_stock_source_changed")
        self.assertEqual(await self.physical_snapshot(), before)
        self.raw_orders.clear()
        with self.assertRaises(source.SourceError):
            await self.apply(preview)
        self.assertEqual(await self.physical_snapshot(), before)

    async def test_stale_statuses_policy_local_revision_plan_and_expired_preview_require_review(self):
        await self.seed("SKU-A", "5")
        raw = raw_order()
        first, second = await self.preview(raw), await self.preview(raw)
        await self.apply(first)
        before = await self.physical_snapshot()
        with self.assertRaises(service.OrderStockError) as raised:
            await self.apply(second)
        self.assertEqual(raised.exception.code, "order_stock_revision_changed")
        self.assertEqual(await self.physical_snapshot(), before)
        current = await self.preview(raw)
        await self.configure(actions={**ACTIONS, "19": "review"})
        with self.assertRaises(service.OrderStockError) as raised:
            await self.apply(current)
        self.assertEqual(raised.exception.code, "order_stock_policy_changed")
        current = await self.preview(raw)
        self.statuses["order_statuses"][0]["mark_paid_yn"] = True
        with self.assertRaises(service.OrderStockError):
            await self.apply(current)
        self.statuses = copy.deepcopy(STATUSES)
        current = await self.preview(raw)
        self.clock.return_value = NOW + timedelta(minutes=31)
        with self.assertRaises(service.OrderStockError) as raised:
            await self.apply(current)
        self.assertEqual(raised.exception.code, "order_stock_preview_expired")
        self.clock.return_value = NOW
        current = await self.preview(raw)
        async with self.sessions() as db:
            await db.execute(update(StockBalance).where(StockBalance.product_id == self.products["SKU-A"])
                             .values(qty_on_hand=D("6"), total_value=D("60")))
            await db.commit()
        changed_stock = await self.physical_snapshot()
        with self.assertRaises(service.OrderStockError):
            await self.apply(current)
        self.assertEqual(await self.physical_snapshot(), changed_stock)

    async def test_preview_request_recovery_hash_and_issue_physical_confirmation_are_binding(self):
        await self.seed("SKU-A", "2")
        raw = raw_order(status_id=8)
        request_id = str(uuid4())
        preview = await self.preview(raw, request_id=request_id)
        reads = len(self.source_reads)
        self.assertEqual(await self.preview(raw, request_id=request_id), preview)
        self.assertEqual(len(self.source_reads), reads)
        async with self.sessions() as db:
            self.assertEqual(await service.get_preview(db, preview["id"]), preview)
            self.assertIn(preview["id"], [entry["id"] for entry in (await service.list_previews(db, "biketrek"))["previews"]])
        before = await self.physical_snapshot()
        with self.assertRaises(service.OrderStockError):
            await self.apply(preview, preview_hash="0" * 64)
        with self.assertRaises(service.OrderStockError) as raised:
            await self.apply(preview, physical=False)
        self.assertEqual(raised.exception.code, "order_stock_physical_confirmation_required")
        self.assertEqual(await self.physical_snapshot(), before)
        changed = raw_order(order_number="DIFFERENT")
        with self.assertRaises(service.OrderStockError):
            await self.preview(changed, request_id=request_id)

    async def test_issued_order_locks_changed_lines_cancel_return_and_new_preview_does_not_issue_again(self):
        await self.seed("SKU-A", "3")
        raw = raw_order(status_id=8)
        result = await self.process(raw)
        frozen = await self.physical_snapshot()
        noop = await self.preview(raw)
        self.assertEqual(noop["action"], "noop")
        self.assertEqual(await self.apply(noop), result)
        for changed in ({**raw, "status_id": 2}, {**raw, "status_id": 25},
                        {**raw, "products": [{**raw["products"][0], "quantity": "2"}]}):
            with self.assertRaises(service.OrderStockError):
                await self.preview(changed)
            self.assertEqual(await self.physical_snapshot(), frozen)

    async def test_issued_order_remapped_product_identity_requires_review_instead_of_noop(self):
        await self.seed("SKU-A", "2")
        async with self.sessions() as db:
            mapping = ShopProduct(shop_id=self.shops["biketrek"], product_id=self.products["SKU-A"],
                                  external_code="ORDER-ALIAS", is_variant=False)
            db.add(mapping)
            await db.commit()
            mapping_id = mapping.id
        raw = raw_order(raw_line(code="ORDER-ALIAS"), status_id=8)
        await self.process(raw)
        async with self.sessions() as db:
            await db.execute(update(ShopProduct).where(ShopProduct.id == mapping_id)
                             .values(product_id=self.products["SKU-B"]))
            await db.commit()
        before = await self.physical_snapshot()
        with self.assertRaises(service.OrderStockError) as raised:
            await self.preview(raw)
        self.assertEqual(raised.exception.code, "order_stock_issued_locked")
        self.assertEqual(await self.physical_snapshot(), before)
        self.assertEqual(len(await self.movements()), 1)

    async def race_while_balance_locked(self, first, second):
        async with self.sessions() as held:
            await held.execute(select(StockBalance).where(StockBalance.product_id == self.products["SKU-A"])
                               .with_for_update())
            tasks = [asyncio.create_task(first()), asyncio.create_task(second())]
            try:
                await asyncio.sleep(0.05)
                self.assertTrue(all(not task.done() for task in tasks), "Both operations wait for the held physical balance")
            finally:
                await held.rollback()
            return await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)

    async def test_same_preview_concurrent_issue_returns_one_movement_and_identical_result(self):
        await self.seed("SKU-A", "2")
        preview = await self.preview(raw_order(status_id=8))
        first, second = await self.race_while_balance_locked(lambda: self.apply(preview), lambda: self.apply(preview))
        self.assertIsInstance(first, dict)
        self.assertEqual(second, first)
        self.assertEqual(len(await self.movements()), 1)
        self.assertEqual((await self.balance("SKU-A"))[0], D("1"))

    async def test_different_previews_for_one_order_and_two_orders_competing_never_double_issue(self):
        await self.seed("SKU-A", "1")
        raw = raw_order(status_id=8)
        first, second = await self.preview(raw), await self.preview(raw)
        results = await self.race_while_balance_locked(lambda: self.apply(first), lambda: self.apply(second))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1, results)
        self.assertEqual(len(await self.movements()), 1)

    async def test_two_orders_racing_for_one_free_piece_only_one_can_commit(self):
        await self.seed("SKU-A", "1")
        first = await self.preview(raw_order(status_id=8))
        second = await self.preview(raw_order(status_id=8, order_number="OTHER-ISSUE"))
        results = await self.race_while_balance_locked(lambda: self.apply(first), lambda: self.apply(second))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1, results)
        self.assertEqual(len(await self.movements()), 1)
        self.assertEqual((await self.balance("SKU-A"))[:2], (D("0"), D("0")))

    async def test_receipt_racing_with_issue_preserves_quantity_and_total_acquisition_value(self):
        await self.seed("SKU-A", "2", cost="10")
        preview = await self.preview(raw_order(status_id=8))
        async with self.sessions() as db:
            receipt = ReceivingSession(supplier_id=self.supplier_id, warehouse_id=self.warehouse_id,
                invoice_number="ORDER-RECEIPT", status=ReceivingStatus.in_progress, total_lines=1, session_data={})
            db.add(receipt)
            await db.flush()
            db.add(ReceivingLine(session_id=receipt.id, line_number=1, product_id=self.products["SKU-A"],
                                ordered_qty=D("1"), received_qty=D("1"), unit_price=D("20"), status="received"))
            await db.commit()
            receipt_id = receipt.id
        async def receive():
            async with self.sessions() as db:
                try:
                    return await receiving.finalize_session("order-receipt", receipt_id, receiving.FinalizeRequest(), db)
                except Exception:
                    await db.rollback()
                    raise
        issue_result, receipt_result = await self.race_while_balance_locked(lambda: self.apply(preview), receive)
        self.assertIsInstance(receipt_result, dict)
        self.assertIsInstance(issue_result, (dict, service.OrderStockError))
        movements = await self.movements()
        issues = [movement for movement in movements if movement.movement_type == MovementType.SALE_OUT]
        balance = await self.balance("SKU-A")
        self.assertEqual(balance[0], D("3") - len(issues))
        self.assertEqual(balance[3] + sum((movement.total_cost for movement in issues), D("0")), D("40"))
        self.assertEqual(balance[1], D("0"))

    async def test_migration_rerun_preserves_order_ledger_and_policy_cutover(self):
        await self.seed("SKU-A", "2")
        await self.process(raw_order(status_id=8))
        before = await self.physical_snapshot()
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'SET search_path TO "{self.schema}"')
            await connection.execute((self.sql_root / "007_order_stock.sql").read_text())
        finally:
            await connection.close()
        self.assertEqual(await self.physical_snapshot(), before)
        configured = await self.configure(actions={**ACTIONS, "19": "review"})
        self.assertEqual(configured["policy"]["starts_at"], NOW.isoformat())
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(OrderStockPreview)), 1)
