"""Warehouse links never imply physical confirmation or order-stock activation."""
import asyncio
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.db_models import Shop, ShopWarehouse, ShopWarehouseRole, Warehouse
from inventory_hub.db_models_ext import StockMovement
from inventory_hub.order_stock_models import OrderStockPolicy
from inventory_hub.order_stock_types import OrderStockConfigureRequest, ShopWarehouseAssignmentRequest
from inventory_hub.services import order_stock
from inventory_hub.services import shop_warehouse_assignment as service


TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")


@unittest.skipUnless(TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class WarehouseAssignmentDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("Warehouse assignment tests require a dedicated localhost *_catalog_test database")
        self.schema = "warehouse_assignment_test_" + uuid4().hex
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            root = Path(__file__).resolve().parents[2] / "infra" / "db-init"
            for name in ("001_schema.sql", "007_order_stock.sql"):
                await connection.execute((root / name).read_text())
        finally:
            await connection.close()
        self.engine = create_async_engine(TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1),
            poolclass=NullPool, connect_args={"server_settings": {"search_path": self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        async with self.sessions() as db:
            # Remove only this disposable schema's default seed links.
            await db.execute(delete(ShopWarehouse))
            first = Warehouse(code="assigned-main", name="Main")
            second = Warehouse(code="assigned-other", name="Other")
            db.add_all([first, second])
            await db.commit()
            self.first, self.second = first.id, second.id
            self.shops = {s.code: s.id for s in (await db.scalars(select(Shop))).all()}

    async def asyncTearDown(self):
        await self.engine.dispose()
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            await connection.close()

    async def options(self, shop="biketrek"):
        async with self.sessions() as db:
            return await service.assignment_options(db, self.shops[shop])

    async def assign(self, warehouse="assigned-main", shop="biketrek", expected=None):
        if expected is None:
            expected = (await self.options(shop))["assignment_hash"]
        async with self.sessions() as db:
            return await service.assign_warehouse(db, ShopWarehouseAssignmentRequest(
                shop_code=shop, warehouse_code=warehouse, expected_assignment_hash=expected))

    async def test_two_shops_can_share_warehouse_without_policies_or_movements(self):
        before = await self.options()
        result = await self.assign(expected=before["assignment_hash"])
        self.assertEqual(result["warehouse_assignment"]["warehouse_id"], self.first)
        self.assertEqual(await self.assign(expected=before["assignment_hash"]), result)
        await self.assign(shop="xtrek")
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(OrderStockPolicy)), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)), 0)
            links = (await db.scalars(select(ShopWarehouse))).all()
            self.assertEqual(len(links), 2)
            self.assertEqual({link.warehouse_id for link in links}, {self.first})

    async def test_stale_state_and_conflicting_mapping_do_not_overwrite(self):
        initial = await self.options()
        async with self.sessions() as db:
            db.add(ShopWarehouse(shop_id=self.shops["biketrek"], warehouse_id=self.second,
                                role=ShopWarehouseRole.availability_source, priority=17, is_active=True))
            await db.commit()
        with self.assertRaises(service.AssignmentError) as error:
            await self.assign(expected=initial["assignment_hash"])
        self.assertEqual(error.exception.code, "warehouse_assignment_changed")
        await self.assign()
        with self.assertRaises(service.AssignmentError) as error:
            await self.assign(warehouse="assigned-other")
        self.assertEqual(error.exception.code, "warehouse_assignment_conflict")
        async with self.sessions() as db:
            other = await db.scalar(select(ShopWarehouse).where(ShopWarehouse.warehouse_id == self.second))
            self.assertEqual((other.role, other.priority, other.is_active),
                             (ShopWarehouseRole.availability_source, 17, True))

    async def test_policy_conflict_preserves_cutoff_and_never_adds_link(self):
        cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
        async with self.sessions() as db:
            db.add(OrderStockPolicy(shop_id=self.shops["biketrek"], warehouse_id=self.second,
                starts_at=cutoff, revision=7, status_actions={"1": "review"}, status_hash="a" * 64, statuses=[]))
            await db.commit()
        with self.assertRaises(service.AssignmentError) as error:
            await self.assign()
        self.assertEqual(error.exception.code, "warehouse_assignment_policy_conflict")
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(ShopWarehouse)), 0)
            policy = await db.get(OrderStockPolicy, self.shops["biketrek"])
            self.assertEqual((policy.starts_at, policy.revision, policy.status_actions),
                             (cutoff, 7, {"1": "review"}))
        await self.assign(warehouse="assigned-other")

    async def test_later_first_activation_cannot_conflict_with_saved_assignment(self):
        await self.assign()
        statuses = {"statuses": [{"id": 1, "name": "New", "type": "Custom"}], "status_hash": "b" * 64}
        with patch.object(order_stock, "load_statuses", AsyncMock(return_value=statuses)):
            async with self.sessions() as db:
                with self.assertRaises(service.AssignmentError) as error:
                    await order_stock.configure(db, OrderStockConfigureRequest(
                        shop_code="biketrek", warehouse_code="assigned-other", status_hash="b" * 64,
                        status_actions={"1": "review"}, confirmed=True))
                self.assertEqual(error.exception.code, "warehouse_assignment_conflict")
        async with self.sessions() as db:
            self.assertIsNone(await db.get(OrderStockPolicy, self.shops["biketrek"]))

    async def test_competing_initial_assignments_serialize(self):
        expected = (await self.options())["assignment_hash"]
        outcomes = await asyncio.gather(self.assign(expected=expected),
            self.assign(warehouse="assigned-other", expected=expected), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, dict) for result in outcomes), 1)
        failures = [result for result in outcomes if isinstance(result, Exception)]
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], service.AssignmentError)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(ShopWarehouse)), 1)

    async def test_inactive_warehouse_or_shop_cannot_be_assigned(self):
        async with self.sessions() as db:
            warehouse = await db.get(Warehouse, self.first)
            warehouse.is_active = False
            await db.commit()
        with self.assertRaises(service.AssignmentError) as error:
            await self.assign()
        self.assertEqual(error.exception.code, "order_stock_warehouse_unavailable")
        async with self.sessions() as db:
            shop = await db.get(Shop, self.shops["biketrek"])
            shop.is_active = False
            await db.commit()
        with self.assertRaises(service.AssignmentError) as error:
            await self.assign(warehouse="assigned-other")
        self.assertEqual(error.exception.code, "order_stock_shop_not_found")
