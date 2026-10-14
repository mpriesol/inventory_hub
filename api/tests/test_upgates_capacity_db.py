"""Capacity counts real local evidence without enabling or mutating anything."""
import os
from datetime import timedelta
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.db_models import MovementType, Product, Shop, Warehouse
from inventory_hub.db_models_ext import ShopProduct, StockBalance, StockMovement
from inventory_hub.order_collection_models import OrderCollectionSettings, OrderInboxEntry
from inventory_hub.order_processing_models import OrderProcessingJob
from inventory_hub.order_stock_models import OrderStockPolicy
from inventory_hub.stock_settings_models import StockShopSettings, StockWarehouseSettings
from inventory_hub.stock_sync_models import StockSyncSettings, StockSyncWarehouseSettings
from inventory_hub.services import order_collection, upgates_capacity as service
from stock_tracking_fixture import confirmed_inventory

TEST_URL = os.environ.get('CATALOG_TEST_DATABASE_URL', '')


@unittest.skipUnless(TEST_URL, 'Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB')
class CapacityDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ('localhost', '127.0.0.1') or not parsed.path.endswith('_catalog_test'):
            raise RuntimeError('Capacity tests require isolated localhost *_catalog_test')
        self.schema = 'capacity_test_' + uuid4().hex
        connection = await asyncpg.connect(TEST_URL)
        root = Path(__file__).resolve().parents[2] / 'infra' / 'db-init'
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            for name in ('001_schema.sql', '007_order_stock.sql', '008_order_collection.sql',
                         '009_stock_automation.sql', '011_fifo.sql', '015_stock_sync.sql', '019_stock_tracking.sql'):
                await connection.execute((root / name).read_text())
        finally:
            await connection.close()
        self.engine = create_async_engine(TEST_URL.replace('postgresql://', 'postgresql+asyncpg://', 1),
            poolclass=NullPool, connect_args={'server_settings': {'search_path': self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=True)
        self.clock = service.now()
        self.config_patch = patch.object(order_collection, 'load_shop', return_value={
            'upgates_api_base_url': 'https://capacity-test.invalid/api/v2',
            'upgates_login': 'synthetic-test', 'upgates_api_key': 'synthetic-key'})
        self.config_patch.start()
        async with self.sessions() as db:
            self.shop_id = await db.scalar(select(Shop.id).where(Shop.code == 'biketrek'))
            warehouse = Warehouse(code='capacity-central', name='Capacity central')
            db.add(warehouse)
            await db.flush()
            self.warehouse_id = warehouse.id
            await db.commit()

    async def asyncTearDown(self):
        self.config_patch.stop()
        await self.engine.dispose()
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            await connection.close()

    async def seed_products(self):
        async with self.sessions() as db:
            for code, active, listed, confirmed, quantity in (
                    ('VALID', True, True, True, 4), ('ZERO', True, True, True, 0),
                    ('UNVERIFIED', True, True, False, 7), ('INACTIVE', False, True, True, 1),
                    ('ALIAS', True, True, True, 1), ('UNLISTED', True, False, True, 2)):
                product = Product(sku=code, name=code, is_active=active)
                db.add(product)
                await db.flush()
                db.add(ShopProduct(shop_id=self.shop_id, product_id=product.id, is_listed=listed,
                    external_code='ALIAS-REMOTE' if code == 'ALIAS' else code, is_variant=False))
                db.add(StockBalance(product_id=product.id, warehouse_id=self.warehouse_id,
                    qty_on_hand=quantity, qty_reserved=0, qty_quarantined=0, avg_cost=1, total_value=quantity))
                if confirmed:
                    db.add(confirmed_inventory(product.id, self.warehouse_id))
                    if quantity:
                        db.add(StockMovement(product_id=product.id, warehouse_id=self.warehouse_id,
                            movement_type=MovementType.INITIAL, quantity=quantity, unit_cost=1,
                            balance_after=quantity, avg_cost_after=1, idempotency_key=uuid4().hex))
            # An unmapped Hub card is not part of the publisher's listed catalog.
            db.add(Product(sku='HUB-ONLY', name='Hub only'))
            await db.commit()

    async def snapshot(self):
        tables = ('products', 'shop_products', 'stock_balances', 'stock_movements', 'stock_tracking',
                  'stock_sync_settings', 'stock_sync_warehouse_settings', 'stock_sync_runs', 'stock_sync_items',
                  'order_stock_policies', 'stock_shop_settings', 'stock_warehouse_settings',
                  'order_collection_settings', 'order_inbox', 'order_processing_jobs', 'shop_sync_outbox')
        async with self.sessions() as db:
            return {table: list((await db.execute(text(
                f'SELECT row_to_json(t) AS row FROM {table} t ORDER BY row_to_json(t)::text'))).scalars())
                for table in tables}

    async def test_without_policy_reports_unknown_eligibility_and_still_probes_after_read_transaction(self):
        await self.seed_products()
        before = await self.snapshot()
        async with self.sessions() as db:
            def status_probe(shop_code):
                self.assertEqual(shop_code, 'biketrek')
                self.assertFalse(db.in_transaction(), 'No database transaction across status GET')
                return {'observed_at': self.clock.isoformat(), 'probe': {'ok': True, 'requests_attempted': 1},
                        'quota': {'total_remaining': None}}
            with patch.object(service, 'probe', Mock(side_effect=status_probe)) as probe:
                report = await service.diagnose(db, 'biketrek')
            probe.assert_called_once_with('biketrek')
        local = report['projection']
        self.assertEqual(local['listed_skus'], 5)
        for field in ('warehouse_code', 'confirmed_skus', 'eligible_skus', 'unpublishable_skus'):
            self.assertIsNone(local[field], field)
        self.assertFalse(local['stock']['configured'])
        self.assertFalse(local['stock']['enabled'])
        self.assertFalse(local['stock']['authorized'])
        self.assertIsNone(local['stock']['unchanged_pass_calls'])
        self.assertIsNone(local['stock']['daily_all_changed_if_enabled'])
        self.assertEqual(local['stock']['interval_seconds'], 300)
        self.assertEqual(local['orders']['processing_mode'], 'manual')
        self.assertFalse(local['orders']['processing_ready'])
        self.assertEqual(local['orders']['processing_error'], 'stock_settings_policy_required')
        self.assertEqual(local['orders']['collector_daily_if_enabled'], 576)
        self.assertEqual(await self.snapshot(), before)

    async def test_real_eligible_leaves_effective_intervals_and_order_workload_without_writes(self):
        await self.seed_products()
        async with self.sessions() as db:
            db.add(OrderStockPolicy(shop_id=self.shop_id, warehouse_id=self.warehouse_id,
                starts_at=self.clock - timedelta(days=30), revision=1, status_actions={}, status_hash='a' * 64, statuses=[]))
            db.add(StockSyncWarehouseSettings(warehouse_id=self.warehouse_id, interval_seconds=900))
            db.add(StockSyncSettings(shop_id=self.shop_id, warehouse_id=self.warehouse_id,
                interval_seconds=600, enabled=False, authorized=False))
            db.add(StockWarehouseSettings(warehouse_id=self.warehouse_id, updated_at=self.clock,
                values={'poll_interval_seconds': 600, 'processing_retry_minutes': 10, 'full_order_check_hours': 48}))
            db.add(StockShopSettings(shop_id=self.shop_id, updated_at=self.clock, mode='reserve',
                overrides={'poll_interval_seconds': 120, 'processing_retry_minutes': 15, 'full_order_check_hours': 12},
                automation_starts_at=self.clock - timedelta(days=1), authorized_policy_revision=1,
                target_fingerprint=order_collection.target_fingerprint('biketrek')))
            db.add(OrderCollectionSettings(shop_id=self.shop_id, enabled=False, revision=1,
                target_fingerprint=order_collection.target_fingerprint('biketrek'), cursor_at=self.clock,
                reconcile_cursor_at=self.clock, next_poll_at=self.clock))
            for index, status in enumerate(('retry', 'retry', 'completed', 'completed', 'completed',
                                             'review', 'pending', 'running', None, None)):
                entry = OrderInboxEntry(shop_id=self.shop_id, source_uuid=str(uuid4()), order_number=f'FIXTURE-{index}',
                    created_at=self.clock - timedelta(days=2 if index == 9 else 0), updated_at=self.clock,
                    deleted=False, origin='eshop', status_id=1, observation_hash='b' * 64,
                    observed_at=self.clock, last_seen_at=self.clock)
                db.add(entry)
                await db.flush()
                if status:
                    db.add(OrderProcessingJob(shop_id=self.shop_id, inbox_id=entry.id, source_uuid=entry.source_uuid,
                        order_number=entry.order_number, observation_hash=entry.observation_hash,
                        status=status, next_attempt_at=self.clock))
            await db.commit()
        before = await self.snapshot()
        with patch.object(service.stock_sync, 'enabled', return_value=False):
            async with self.sessions() as db:
                local = await service.projection(db, 'biketrek')
        self.assertEqual((local['listed_skus'], local['confirmed_skus'], local['eligible_skus'], local['unpublishable_skus']),
                         (5, 4, 2, 3))
        self.assertEqual(local['eligibility_errors'], [
            {'code': 'stock_projection_balance_unverified', 'count': 1},
            {'code': 'stock_projection_mapping_alias', 'count': 1},
            {'code': 'stock_projection_product_inactive', 'count': 1}])
        self.assertEqual(local['stock']['interval_seconds'], 600)
        self.assertEqual((local['stock']['unchanged_pass_calls'], local['stock']['all_changed_pass_calls']), (2, 6))
        self.assertEqual((local['stock']['daily_unchanged_if_enabled'], local['stock']['daily_all_changed_if_enabled']), (288, 864))
        self.assertFalse(local['stock']['enabled'])
        self.assertFalse(local['stock']['server_enabled'])
        orders = local['orders']
        self.assertFalse(orders['collector_enabled'])
        self.assertEqual(orders['jobs_by_status'], {'pending': 1, 'running': 1, 'completed': 3, 'retry': 2, 'review': 1})
        self.assertEqual((orders['inbox_count'], orders['unprocessed_count']), (10, 1))
        self.assertEqual((orders['poll_interval_seconds'], orders['retry_minutes'], orders['full_check_hours']), (120, 15, 12))
        self.assertEqual((orders['collector_daily_if_enabled'], orders['retry_daily_if_processing'],
                          orders['history_daily_if_processing']), (1440, 384, 16))
        self.assertEqual(local['estimate_kind'], 'theoretical_no_runtime_delay')
        self.assertIn('other_integrations', local['not_included'])
        self.assertEqual(await self.snapshot(), before)

    async def test_missing_shop_never_probes_and_staged_local_changes_are_not_flushed(self):
        before = await self.snapshot()
        with patch.object(service, 'probe', Mock()) as probe:
            async with self.sessions() as db:
                with self.assertRaises(service.CapacityError) as raised:
                    await service.diagnose(db, 'missing')
                self.assertEqual(raised.exception.status, 404)
                self.assertEqual(raised.exception.code, 'upgates_capacity_shop_not_found')
            probe.assert_not_called()
        async with self.sessions() as db:
            staged = Product(sku='UNCOMMITTED', name='Uncommitted')
            db.add(staged)
            report = await service.projection(db, 'biketrek')
            self.assertEqual(report['listed_skus'], 0)
            self.assertIsNone(staged.id, 'Read-only counts must not autoflush unrelated writes')
        self.assertEqual(await self.snapshot(), before)
