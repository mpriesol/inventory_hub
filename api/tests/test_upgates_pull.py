"""Product pulls keep shop snapshots separate from physical inventory."""
import os
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.database import get_session
from inventory_hub.db_models import MovementType, Product, Warehouse
from inventory_hub.db_models_ext import ShopProduct, ShopProductContent, StockBalance, StockMovement
from inventory_hub.routers import upgates_sync


META = {"source": "cache", "pulled_at": "2026-09-23T00:00:00", "cache_age_s": 0}
PRODUCTS = [
    {"code": "PULL-SIMPLE", "stock": 7, "prices": [{"price_with_vat": 123}],
     "descriptions": [{"language": "sk", "title": "Test product"}]},
    {"code": "PULL-GROUP", "descriptions": [{"language": "sk", "title": "Test variants"}],
     "variants": [
         {"code": "PULL-M", "stock": 9, "prices": [{"price_with_vat": 246}]},
         {"code": "PULL-L", "stock": 2, "prices": [{"price_with_vat": 369}]},
     ]},
]


class UpgatesPullEndpointTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock(spec=AsyncSession)
        app = FastAPI()
        app.include_router(upgates_sync.router)

        async def session():
            yield self.db

        app.dependency_overrides[get_session] = session
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_legacy_stock_request_is_rejected_before_shop_lookup_network_or_writes(self):
        with patch.object(upgates_sync, "_get_shop", AsyncMock()) as shop, \
                patch.object(upgates_sync, "_fetch_upgates_products") as fetch:
            for include_stock in (True, "true", "false", 1, None, []):
                with self.subTest(include_stock=include_stock):
                    response = self.client.post("/shops/biketrek/upgates/products/import", json={
                        "all": True, "include_stock": include_stock,
                    })
                    self.assertEqual(response.status_code, 400)
                    self.assertIn("include_stock", response.json()["detail"])
            shop.assert_not_awaited()
            fetch.assert_not_called()
            self.db.execute.assert_not_called()
            self.db.add.assert_not_called()
            self.db.flush.assert_not_called()

    def test_omitted_or_false_stock_option_accepts_product_only_requests(self):
        with patch.object(upgates_sync, "_get_shop", AsyncMock(return_value=SimpleNamespace(id=1))), \
                patch.object(upgates_sync, "_fetch_upgates_products", return_value=([], META)) as fetch, \
                patch.object(upgates_sync, "_known_skus", AsyncMock(return_value=set())):
            for payload in ({"all": True}, {"all": True, "include_stock": False}):
                response = self.client.post("/shops/biketrek/upgates/products/import", json=payload)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["stock_initialized"], 0)
            self.assertEqual(fetch.call_count, 2)
            self.db.execute.assert_not_called()
            self.db.add.assert_not_called()


TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")


@unittest.skipUnless(TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class UpgatesPullDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("Pull tests require a dedicated localhost *_catalog_test database")
        self.schema = "upgates_pull_test_" + uuid4().hex
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            root = Path(__file__).resolve().parents[2]
            for name in ("001_schema.sql", "004_shop_product_content.sql"):
                await connection.execute((root / "infra" / "db-init" / name).read_text())
        finally:
            await connection.close()
        self.engine = create_async_engine(
            TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1), poolclass=NullPool,
            connect_args={"server_settings": {"search_path": self.schema}},
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        self.fetcher = patch.object(upgates_sync, "_fetch_upgates_products", return_value=(PRODUCTS, META))
        self.fetcher.start()

    async def asyncTearDown(self):
        self.fetcher.stop()
        await self.engine.dispose()

    async def pull(self, shop="biketrek", **options):
        async with self.sessions() as db:
            result = await upgates_sync.import_upgates_products(shop, {"all": True, **options}, db)
            await db.commit()
            self.assertEqual(result["stock_initialized"], 0)
            return result

    async def stock_snapshot(self):
        async with self.sessions() as db:
            return {
                table: (await db.execute(text(f"SELECT * FROM {table} ORDER BY id"))).mappings().all()
                for table in ("stock_balances", "stock_movements")
            }

    async def test_repeated_two_shop_pull_keeps_quantities_and_prices_as_snapshots_only(self):
        # Product-only pulls work before a default physical warehouse is configured.
        async with self.sessions() as db:
            await db.execute(update(Warehouse).values(is_default=False))
            await db.commit()
        first = await self.pull()
        self.assertEqual((first["created_products"], first["created_variants"]), (2, 2))
        await self.pull()
        await self.pull(update_existing=True)
        await self.pull("xtrek", update_existing=True, include_stock=False)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(Product)), 3)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ShopProductContent)), 4)
            mappings = (await db.execute(select(ShopProduct))).scalars().all()
            self.assertEqual(len(mappings), 6)
            for mapping in mappings:
                expected = {
                    "PULL-SIMPLE": (Decimal("7"), Decimal("123")),
                    "PULL-M": (Decimal("9"), Decimal("246")),
                    "PULL-L": (Decimal("2"), Decimal("369")),
                }[mapping.variant_code or mapping.external_code]
                self.assertEqual((mapping.shop_stock, mapping.shop_price), expected)
        self.assertEqual(await self.stock_snapshot(), {"stock_balances": [], "stock_movements": []})

    async def test_pull_preserves_existing_ledger_and_acquisition_values_exactly(self):
        async with self.sessions() as db:
            product = Product(sku="PULL-SIMPLE", name="Already received")
            db.add(product)
            await db.flush()
            warehouse = await db.scalar(select(Warehouse.id))
            movement = StockMovement(
                idempotency_key="fixture-receipt", product_id=product.id, warehouse_id=warehouse,
                movement_type=MovementType.RECEIVING_IN, quantity=Decimal("3"),
                unit_cost=Decimal("8.50"), balance_after=Decimal("3"), avg_cost_after=Decimal("8.50"),
            )
            db.add(movement)
            await db.flush()
            db.add(StockBalance(
                product_id=product.id, warehouse_id=warehouse, qty_on_hand=Decimal("3"),
                qty_reserved=Decimal("1"), avg_cost=Decimal("8.50"), total_value=Decimal("25.50"),
                last_purchase_price=Decimal("8.50"), last_movement_id=movement.id,
            ))
            await db.commit()
        before = await self.stock_snapshot()
        await self.pull(update_existing=True)
        await self.pull("xtrek", update_existing=True)
        self.assertEqual(await self.stock_snapshot(), before)
