"""Private PostgreSQL/file fixtures for the external invoice archive tests."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from current_schema_fixture import apply_current_core_schema
from inventory_hub import config_io
from inventory_hub.settings import settings


TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")
DATABASE_REQUIRED = "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB"


class InvoiceDatabaseCase(unittest.IsolatedAsyncioTestCase):
    """A new guarded schema and synthetic document directory for every test."""

    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("Invoice tests require a dedicated localhost *_catalog_test database")
        self.schema = "invoice_test_" + uuid4().hex
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            self.addAsyncCleanup(self._drop_schema)
            await connection.execute(f'SET search_path TO "{self.schema}"')
            root = Path(__file__).resolve().parents[2] / "infra" / "db-init"
            await connection.execute((root / "001_schema.sql").read_text(encoding="utf-8"))
            # Historical migration002 checks pg_type globally, not per schema.
            await connection.execute("CREATE TYPE payment_status AS ENUM ('unpaid', 'partial', 'paid')")
            for name in ("002_invoice_management.sql", "003_uploaded_invoices.sql", "006_opening_stock.sql",
                         "007_order_stock.sql"):
                await connection.execute((root / name).read_text(encoding="utf-8"))
            await apply_current_core_schema(connection)
            await connection.execute((root / "012_product_editor.sql").read_text(encoding="utf-8"))
            await connection.execute((root / "033_invoice_archive.sql").read_text(encoding="utf-8"))
        finally:
            await connection.close()
        self.engine = create_async_engine(
            TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1),
            poolclass=NullPool, connect_args={"server_settings": {"search_path": self.schema}},
        )
        self.addAsyncCleanup(self.engine.dispose)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_root = Path(temporary.name)
        for target, name in ((settings, "INVENTORY_DATA_ROOT"), (config_io, "DATA_ROOT")):
            patcher = patch.object(target, name, self.data_root)
            patcher.start()
            self.addCleanup(patcher.stop)
        async with self.sessions() as db:
            self.supplier_id = await db.scalar(text(
                "INSERT INTO suppliers(code,name) VALUES ('archive-fixture','Invoice fixture supplier') RETURNING id"))
            self.other_supplier_id = await db.scalar(text(
                "INSERT INTO suppliers(code,name) VALUES ('archive-other','Other fixture supplier') RETURNING id"))
            self.shop_id = await db.scalar(text(
                "INSERT INTO shops(code,name,platform) VALUES ('archive-shop','Invoice fixture shop','upgates') RETURNING id"))
            self.product_id = await db.scalar(text(
                "INSERT INTO products(sku,name) VALUES ('ARCHIVE-1','Invoice trace fixture') RETURNING id"))
            await db.commit()

    async def _drop_schema(self):
        connection = await asyncpg.connect(TEST_URL)
        try:
            # Only this test's random schema in the explicitly guarded database.
            await connection.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            await connection.close()

    async def scalar(self, sql, values=None):
        async with self.sessions() as db:
            return await db.scalar(text(sql), values or {})

    async def rows(self, sql, values=None):
        async with self.sessions() as db:
            return (await db.execute(text(sql), values or {})).mappings().all()

    async def assert_no_inventory_effects(self):
        for table in ("stock_movements", "stock_balances", "reservations", "fifo_layers", "fifo_allocations",
                      "receiving_sessions", "receiving_lines", "shop_orders", "shop_sync_outbox"):
            with self.subTest(physical_table=table):
                self.assertEqual(await self.scalar(f"SELECT count(*) FROM {table}"), 0)
