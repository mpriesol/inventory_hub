"""Shared dictionaries and generic overrides with no stock or shop side effects."""
import os
from pathlib import Path
import unittest
from urllib.parse import urlsplit
from uuid import uuid4
import asyncpg
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool
from current_schema_fixture import apply_current_core_schema
from inventory_hub.db_models import Product, Warehouse
from inventory_hub.db_models_ext import StockMovement
from inventory_hub.product_editor_models import ProductEditorAudit, ProductEditorOverride
from inventory_hub.services import availability_catalog as service
from inventory_hub.setting_catalog_types import AvailabilityChange, WarehouseChange

TEST_URL = os.environ.get('CATALOG_TEST_DATABASE_URL', '')


@unittest.skipUnless(TEST_URL, 'Requires isolated localhost *_catalog_test database')
class SettingCatalogDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ('localhost', '127.0.0.1') or not parsed.path.endswith('_catalog_test'):
            raise RuntimeError('Requires dedicated localhost *_catalog_test database')
        self.schema = 'setting_catalog_' + uuid4().hex
        root = Path(__file__).resolve().parents[2] / 'infra' / 'db-init'
        conn = await asyncpg.connect(TEST_URL)
        try:
            await conn.execute(f'CREATE SCHEMA "{self.schema}"')
            await conn.execute(f'SET search_path TO "{self.schema}"')
            await conn.execute((root / '001_schema.sql').read_text())
            await conn.execute("CREATE TYPE payment_status AS ENUM ('unpaid', 'partial', 'paid')")
            for filename in ('002_invoice_management.sql', '007_order_stock.sql', '012_product_editor.sql'):
                await conn.execute((root / filename).read_text())
            await apply_current_core_schema(conn)
            for filename in ('010_stock_publication.sql', '015_stock_sync.sql', '025_stock_native.sql', '026_stock_delta.sql',
                             '036_stock_sync_dirty.sql', '054_mixed_receiving.sql', '056_receiving_manual.sql', '057_setting_catalogs.sql'):
                await conn.execute((root / filename).read_text())
        finally:
            await conn.close()
        self.engine = create_async_engine(TEST_URL.replace('postgresql://', 'postgresql+asyncpg://', 1), poolclass=NullPool,
            connect_args={'server_settings': {'search_path': self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        async with self.sessions() as db:
            card = Product(sku='CAT-1', name='Dictionary fixture')
            db.add(card); await db.flush(); self.product_id = card.id
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        conn = await asyncpg.connect(TEST_URL)
        try:
            await conn.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            await conn.close()

    async def test_dictionary_rename_deactivate_and_replay_preserve_old_explicit_intent(self):
        request = AvailabilityChange(request_id=uuid4(), expected_revision=0, label='do 9 dní')
        async with self.sessions() as db:
            created = await service.save_value(db, request, actor='test')
            self.assertEqual(created, await service.save_value(db, request, actor='test'))
            await service.apply_product_overrides(db, [{'product_id': self.product_id, 'expected_revision': 0, 'label': 'do 9 dní', 'visible': False}], uuid4(), 'test')
            await db.commit()
            changed = await service.save_value(db, AvailabilityChange(request_id=uuid4(), id=created['id'],
                expected_revision=1, label='do 10 dní', is_active=False), actor='test')
            self.assertFalse(changed['is_active'])
            self.assertNotIn('do 10 dní', await service.active_labels(db))
            manual = (await service.get_product_overrides(db, [self.product_id]))[self.product_id]
            self.assertEqual(manual['label'], 'do 9 dní'); self.assertIs(manual['visible'], False)
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)), 0)

    async def test_override_is_atomic_idempotent_conflict_checked_and_clearable(self):
        request = uuid4()
        changes = [{'product_id': self.product_id, 'expected_revision': 0, 'label': 'SKLADOM', 'visible': False}]
        async with self.sessions() as db:
            first = await service.apply_product_overrides(db, changes, request, 'test')
            await db.commit()
            self.assertEqual(await service.apply_product_overrides(db, changes, request, 'test'), first)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductEditorAudit)), 1)
            with self.assertRaisesRegex(service.CatalogError, 'catalog_changed'):
                await service.apply_product_overrides(db, changes, uuid4(), 'test')
            await db.rollback()
            await service.apply_product_overrides(db, [{'product_id': self.product_id, 'expected_revision': 1, 'clear': True}], uuid4(), 'test')
            await db.commit()
            self.assertEqual((await service.get_product_overrides(db, [self.product_id]))[self.product_id], {'revision': 2})
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)), 0)

    async def test_unknown_dictionary_choice_is_rejected_without_partial_override(self):
        async with self.sessions() as db:
            with self.assertRaisesRegex(service.CatalogError, 'availability_value_missing'):
                await service.apply_product_overrides(db, [{'product_id': self.product_id, 'expected_revision': 0, 'label': 'invented'}], uuid4(), 'test')
            self.assertIsNone(await db.get(ProductEditorOverride, self.product_id))

    async def test_warehouse_create_default_switch_deactivate_and_replay(self):
        async with self.sessions() as db:
            first = await service.save_warehouse(db, WarehouseChange(request_id=uuid4(), code='dict-main', name='Main', is_default=True), actor='test')
            request = WarehouseChange(request_id=uuid4(), code='dict-other', name='Other')
            second = await service.save_warehouse(db, request, actor='test')
            self.assertEqual(await service.save_warehouse(db, request, actor='test'), second)
            changed = await service.save_warehouse(db, WarehouseChange(request_id=uuid4(), code='dict-other', name='Other', is_default=True,
                expected_updated_at=second['updated_at']), actor='test')
            main = await db.scalar(select(Warehouse).where(Warehouse.code == 'dict-main').execution_options(populate_existing=True))
            self.assertFalse(main.is_default)
            inactive = await service.save_warehouse(db, WarehouseChange(request_id=uuid4(), code='dict-main', name='Main', is_active=False,
                expected_updated_at=main.updated_at), actor='test')
            self.assertFalse(inactive['is_active'])
            self.assertIsNotNone(await db.get(Warehouse, first['id']))
            with self.assertRaisesRegex(service.CatalogError, 'warehouse_default_required'):
                await service.save_warehouse(db, WarehouseChange(request_id=uuid4(), code='dict-other', name='Other', is_active=False,
                    expected_updated_at=changed['updated_at']), actor='test')

    async def test_manual_label_queues_only_existing_authorized_sync_and_clear_queues_again(self):
        async with self.sessions() as db:
            warehouse_id = await db.scalar(select(Warehouse.id).where(Warehouse.is_default.is_(True)))
            shop_id = await db.scalar(text("SELECT id FROM shops WHERE platform='upgates' ORDER BY id LIMIT 1"))
            await db.execute(text("INSERT INTO shop_products(product_id, shop_id, external_code, is_listed) VALUES(:pid,:shop,'MANUAL-1',true)"),
                {'pid': self.product_id, 'shop': shop_id})
            await db.execute(text("INSERT INTO stock_sync_settings(shop_id,warehouse_id,enabled,authorized,protocol) VALUES(:shop,:warehouse,false,false,'delta_v1')"),
                {'shop': shop_id, 'warehouse': warehouse_id})
            await service.apply_product_overrides(db, [{'product_id': self.product_id, 'expected_revision': 0, 'label': 'SKLADOM'}], uuid4(), 'test')
            await db.commit()
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM stock_sync_dirty')), 0)
            await db.execute(text('UPDATE stock_sync_settings SET enabled=true,authorized=true WHERE shop_id=:shop'), {'shop': shop_id})
            await service.apply_product_overrides(db, [{'product_id': self.product_id, 'expected_revision': 1, 'label': 'overíme'}], uuid4(), 'test')
            await db.commit()
            self.assertEqual(await db.scalar(text('SELECT generation FROM stock_sync_dirty')), 1)
            await service.apply_product_overrides(db, [{'product_id': self.product_id, 'expected_revision': 2, 'visible': False}], uuid4(), 'test')
            await db.commit()
            self.assertEqual(await db.scalar(text('SELECT generation FROM stock_sync_dirty')), 1)
            await service.apply_product_overrides(db, [{'product_id': self.product_id, 'expected_revision': 3, 'clear': True}], uuid4(), 'test')
            await db.commit()
            self.assertEqual(await db.scalar(text('SELECT generation FROM stock_sync_dirty')), 2)
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)), 0)

    async def test_open_receipt_batch_prevents_deactivation_but_archive_keeps_history(self):
        async with self.sessions() as db:
            warehouse = await service.save_warehouse(db, WarehouseChange(request_id=uuid4(), code='busy', name='Busy warehouse'), actor='test')
            batch_id = uuid4()
            await db.execute(text("INSERT INTO mixed_receiving_batches(id,warehouse_id,actor) VALUES(:id,:warehouse,'test')"),
                {'id': batch_id, 'warehouse': warehouse['id']})
            await db.commit()
            request = WarehouseChange(request_id=uuid4(), code='busy', name='Busy warehouse', is_active=False,
                expected_updated_at=warehouse['updated_at'])
            with self.assertRaisesRegex(service.CatalogError, 'warehouse_in_use'):
                await service.save_warehouse(db, request, actor='test')
            await db.rollback()
            await db.execute(text('UPDATE mixed_receiving_batches SET archived_at=now() WHERE id=:id'), {'id': batch_id})
            await db.commit()
            inactive = await service.save_warehouse(db, request, actor='test')
            self.assertFalse(inactive['is_active'])
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM mixed_receiving_batches')), 1)

    async def test_migration_replay_preserves_renamed_default_choice(self):
        async with self.sessions() as db:
            choices = (await service.list_values(db))['items']
            original = next(item for item in choices if item['label'] == 'SKLADOM')
            await service.save_value(db, AvailabilityChange(request_id=uuid4(), id=original['id'],
                expected_revision=original['revision'], label='Na predajni', is_active=False), actor='test')
        root = Path(__file__).resolve().parents[2] / 'infra' / 'db-init'
        conn = await asyncpg.connect(TEST_URL)
        try:
            await conn.execute(f'SET search_path TO "{self.schema}"')
            await conn.execute((root / '057_setting_catalogs.sql').read_text())
        finally:
            await conn.close()
        async with self.sessions() as db:
            choices = (await service.list_values(db, include_inactive=True))['items']
            self.assertEqual(len(choices), 4)
            self.assertNotIn('SKLADOM', [item['label'] for item in choices])
            renamed = next(item for item in choices if item['id'] == original['id'])
            self.assertEqual(renamed['label'], 'Na predajni')
            self.assertFalse(renamed['is_active'])
