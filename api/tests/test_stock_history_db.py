"""Local ledger history against a disposable, strictly isolated PostgreSQL schema."""
import os
import unittest
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.db_models import MovementType, Product, Shop, Supplier, Warehouse
from inventory_hub.db_models_ext import ReceivingSession, ShopOrder, StockMovement
from inventory_hub.fifo_models import FifoReceipt
from inventory_hub.opening_stock_models import OpeningStockBatch
from inventory_hub.product_editor_models import ProductEditorOverride
from inventory_hub.services import stock_history as service


TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")
STAMP = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)


@unittest.skipUnless(TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class StockHistoryDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("Stock history tests require a dedicated localhost *_catalog_test database")
        self.schema = "stock_history_test_" + uuid4().hex
        sql_root = Path(__file__).resolve().parents[2] / "infra" / "db-init"
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            await connection.execute((sql_root / "001_schema.sql").read_text())
            await connection.execute("CREATE TYPE payment_status AS ENUM ('unpaid', 'partial', 'paid')")
            for filename in ("002_invoice_management.sql", "006_opening_stock.sql", "007_order_stock.sql",
                             "011_fifo.sql", "012_product_editor.sql"):
                await connection.execute((sql_root / filename).read_text())
        finally:
            await connection.close()
        self.engine = create_async_engine(TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1),
            poolclass=NullPool, connect_args={"server_settings": {"search_path": self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        async with self.transaction() as db:
            central = Warehouse(code="history-central", name="History central")
            other = Warehouse(code="history-other", name="History other")
            supplier = Supplier(code="history-supplier", name="History supplier")
            products = [Product(sku=sku, name="Original " + sku) for sku in ("SKU-1", "SKU-10", "sku-1")]
            db.add_all([central, other, supplier, *products])
            await db.flush()
            self.central, self.other, self.supplier = central.id, other.id, supplier.id
            self.products = {row.sku: row.id for row in products}
            self.shops = {row.code: row.id for row in (await db.scalars(select(Shop))).all()}

    async def asyncTearDown(self):
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

    async def add(self, *, sku="SKU-1", warehouse=None, stamp=STAMP, quantity="1", balance="1", cost=None, **values):
        async with self.transaction() as db:
            row = StockMovement(idempotency_key=uuid4().hex, product_id=self.products[sku],
                warehouse_id=warehouse or self.central,
                movement_type=MovementType.SALE_OUT if D(quantity) < 0 else MovementType.RECEIVING_IN,
                quantity=D(quantity), balance_after=D(balance), created_at=stamp,
                unit_cost=D(cost) if cost is not None else None, **values)
            db.add(row)
            await db.flush()
            return row.id

    async def listing(self, **filters):
        async with self.sessions() as db:
            return await service.list_movements(db, **filters)

    async def snapshot(self):
        async with self.sessions() as db:
            return {table: (await db.execute(text(f"SELECT * FROM {table} ORDER BY id"))).mappings().all()
                for table in ("stock_movements", "stock_balances", "shop_sync_outbox", "shop_orders", "receiving_sessions")}

    async def test_exact_sku_and_warehouse_never_include_similar_or_case_changed_codes(self):
        selected = await self.add()
        await self.add(sku="SKU-10")
        await self.add(sku="sku-1")
        await self.add(warehouse=self.other)
        result = await self.listing(sku="SKU-1", warehouse_code="history-central")
        self.assertEqual([row["id"] for row in result["items"]], [selected])
        self.assertEqual((await self.listing(sku="missing"))["total"], 0)

    async def test_snapshot_pagination_stays_stable_after_new_and_backdated_appends(self):
        ids = [await self.add(stamp=STAMP) for _ in range(31)]
        first = await self.listing(page_size=25)
        await self.add(stamp=STAMP + timedelta(days=1))
        await self.add(stamp=STAMP - timedelta(days=1))
        second = await self.listing(page=2, page_size=25, snapshot_id=first["snapshot_id"])
        self.assertEqual((first["total"], second["total"]), (31, 31))
        self.assertEqual([row["id"] for row in first["items"] + second["items"]], list(reversed(ids)))
        refreshed = await self.listing(page_size=100)
        self.assertEqual(refreshed["total"], 33)
        self.assertEqual((await self.listing(snapshot_id=0))["total"], 0)

    async def test_utc_date_range_includes_full_end_day_and_filters_movement_type(self):
        midnight = STAMP.replace(hour=0)
        await self.add(stamp=midnight - timedelta(microseconds=1))
        first = await self.add(stamp=midnight)
        last = await self.add(stamp=midnight + timedelta(days=1) - timedelta(microseconds=1))
        await self.add(stamp=midnight + timedelta(days=1))
        await self.add(quantity="-1", balance="0")
        result = await self.listing(date_from=date(2026, 9, 24), date_to=date(2026, 9, 24),
            movement_type=MovementType.RECEIVING_IN)
        self.assertEqual([row["id"] for row in result["items"]], [last, first])

    async def test_invoice_and_order_references_are_human_readable_searchable_and_read_only(self):
        async with self.transaction() as db:
            receiving = ReceivingSession(supplier_id=self.supplier, warehouse_id=self.central,
                invoice_number="INV-HISTORY-2026")
            order = ShopOrder(shop_id=self.shops["biketrek"], external_id="upgates-47",
                external_code="BT-2026-0047", order_date=STAMP)
            db.add_all([receiving, order])
            await db.flush()
            receiving_id, order_id = receiving.id, order.id
        receipt = await self.add(reference_type="receiving_session", reference_id=str(receiving_id),
            reference_source="supplier:INV-HISTORY-2026")
        sale = await self.add(quantity="-1", balance="0", reference_type="shop_order", reference_id=str(order_id))
        before = await self.snapshot()
        received = (await self.listing(q="INV-HISTORY"))["items"]
        sold = (await self.listing(q="BT-2026"))["items"]
        self.assertEqual([row["id"] for row in received], [receipt])
        self.assertEqual((received[0]["document"], received[0]["reference_label"], received[0]["supplier_code"]),
            ("INV-HISTORY-2026", "INV-HISTORY-2026", "history-supplier"))
        self.assertEqual([row["id"] for row in sold], [sale])
        self.assertEqual((sold[0]["reference_label"], sold[0]["shop_code"]), ("BT-2026-0047", "biketrek"))
        self.assertEqual(await self.snapshot(), before)

    async def test_documented_receipt_and_opening_keep_full_document_reference(self):
        reference = "Document " + "x" * 110 + " tail-reference"
        receipt_id, opening_id = str(uuid4()), str(uuid4())
        async with self.transaction() as db:
            db.add(FifoReceipt(id=receipt_id, product_id=self.products["SKU-1"], warehouse_id=self.central,
                request_hash="a" * 64, preview_hash="b" * 64, preview_data={"source_reference": reference},
                created_at=STAMP, expires_at=STAMP + timedelta(hours=1)))
            db.add(OpeningStockBatch(id=opening_id, warehouse_id=self.central, warehouse_code="history-central",
                source_reference="Opening count September", operator_name="operator", counted_at=STAMP,
                created_at=STAMP, expires_at=STAMP + timedelta(hours=1), input_hash="c" * 64,
                preview_hash="d" * 64, preview_data={}))
        receipt = await self.add(reference_type="fifo_receipt", reference_id=receipt_id, reference_source=reference[:100])
        opening = await self.add(reference_type="opening_stock", reference_id=opening_id, reference_source="operator_opening")
        result = (await self.listing(q="tail-reference"))["items"]
        self.assertEqual([row["id"] for row in result], [receipt])
        self.assertEqual((result[0]["document"], result[0]["reference_label"]), (reference, reference))
        result = (await self.listing(q="Opening count"))["items"]
        self.assertEqual([row["id"] for row in result], [opening])
        self.assertEqual(result[0]["reference_label"], "Opening count September")

    async def test_search_escapes_wildcards_and_uses_manual_product_name(self):
        async with self.transaction() as db:
            db.add(ProductEditorOverride(product_id=self.products["SKU-1"], revision=1,
                data={"common": {"name": "Manual visible name"}}))
        selected = await self.add(notes=r"Exact 100%_batch\path")
        await self.add(sku="SKU-10", notes="Exact 100XXbatchXpath")
        for query in ("100%_batch", r"batch\path", "Manual visible"):
            result = await self.listing(q=query)
            self.assertEqual([row["id"] for row in result["items"]], [selected], query)
            self.assertEqual(result["items"][0]["product_name"], "Manual visible name")

    async def test_recorded_unknown_and_zero_costs_are_distinct_and_issue_sign_is_preserved(self):
        incoming = await self.add(quantity="5", balance="5")
        outgoing = await self.add(quantity="-2", balance="3", cost="0", total_cost=D("0"))
        rows = {row["id"]: row for row in (await self.listing())["items"]}
        self.assertIsNone(rows[incoming]["unit_cost"])
        self.assertIsNone(rows[incoming]["total_cost"])
        self.assertEqual(D(rows[incoming]["balance_before"]), D("0"))
        self.assertEqual(D(rows[outgoing]["balance_before"]), D("5"))
        self.assertEqual(D(rows[outgoing]["balance_after"]), D("3"))
        self.assertEqual(D(rows[outgoing]["quantity"]), D("-2"))
        self.assertEqual(D(rows[outgoing]["unit_cost"]), D("0"))
        self.assertEqual(D(rows[outgoing]["total_cost"]), D("0"))

