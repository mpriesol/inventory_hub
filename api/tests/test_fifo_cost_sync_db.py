"""Isolated PostgreSQL gates for durable FIFO price publication and recovery."""
import copy
import asyncio
import os
import unittest
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit
from uuid import uuid4
import asyncpg
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from inventory_hub.db_models import Product, Shop, Warehouse
from inventory_hub.db_models_ext import ShopOrder, ShopProduct
from inventory_hub.fifo_cost_models import FifoCostPublication as Publication, FifoCostSettings as Policy
from inventory_hub.fifo_cost_types import CostShopInput, CostWarehouseInput, CostRunInput, CostOrderPreviewInput, CostResolveInput
from inventory_hub.services import fifo_cost_sync as service

TEST_URL = os.environ.get('CATALOG_TEST_DATABASE_URL', '')


@unittest.skipUnless(TEST_URL, 'Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB')
class FifoCostSyncDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ('localhost', '127.0.0.1') or not parsed.path.endswith('_catalog_test'):
            raise RuntimeError('FIFO cost tests require isolated localhost *_catalog_test')
        self.schema = 'fifo_cost_sync_test_' + uuid4().hex
        connection = await asyncpg.connect(TEST_URL)
        root = Path(__file__).resolve().parents[2] / 'infra' / 'db-init'
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            await connection.execute((root / '001_schema.sql').read_text())
            await connection.execute("CREATE TYPE payment_status AS ENUM ('unpaid','partial','paid')")
            for name in ('002_invoice_management.sql', '005_ai_content.sql', '006_opening_stock.sql',
                         '007_order_stock.sql', '008_order_collection.sql', '009_stock_automation.sql',
                         '010_stock_publication.sql', '011_fifo.sql', '012_product_editor.sql',
                         '015_stock_sync.sql', '016_product_publication.sql', '017_stock_adjustments.sql',
                         '018_fifo_cost_sync.sql'):
                await connection.execute((root / name).read_text())
        finally:
            await connection.close()
        self.engine = create_async_engine(TEST_URL.replace('postgresql://', 'postgresql+asyncpg://', 1),
            poolclass=NullPool, connect_args={'server_settings': {'search_path': self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        self.clock = service.now()
        self.product_version = 1
        self.product_cost = '10'
        self.io_session = None
        self.read_hook = None
        self.write_error = None
        self.reads = 0
        self.remote = {'kind': 'product', 'identity': {'code': 'SKU-A', 'parent_code': 'POS-xTrek',
            'variant_code': 'SKU-A', 'product_id': 90, 'variant_id': 91},
            'options': {'prices_with_vat': False, 'language': 'sk', 'currency': 'EUR', 'pricelist': 'Default'},
            'vat': '23', 'guard': 'a' * 64, 'values': {'price_purchase': '2'}}
        self.patches = [patch.object(service, 'now', side_effect=lambda: self.clock),
            patch.object(service, 'enabled', return_value=True),
            patch.object(service, 'target_fingerprint', return_value='f' * 64),
            patch.object(service.projection, 'product_cost', AsyncMock(side_effect=self.project_product)),
            patch.object(service.projection, 'order_cost', AsyncMock(side_effect=self.project_order)),
            patch.object(service.source, 'read_product', AsyncMock(side_effect=self.read_product)),
            patch.object(service.source, 'read_order', AsyncMock(side_effect=self.read_order)),
            patch.object(service.source, 'write_once', AsyncMock(side_effect=self.write_remote))]
        for item in self.patches:
            item.start()
        async with self.session() as db:
            self.shop_id = await db.scalar(select(Shop.id).where(Shop.code == 'biketrek'))
            warehouse = Warehouse(code='cost-central', name='Cost central')
            product = Product(sku='SKU-A', name='SKU-A')
            db.add_all([warehouse, product]); await db.flush()
            self.warehouse_id, self.product_id = warehouse.id, product.id
            db.add(ShopProduct(shop_id=self.shop_id, product_id=product.id, external_code='POS-xTrek',
                parent_code='POS-xTrek', variant_code='SKU-A', is_variant=True, external_id='91'))

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        await self.engine.dispose()
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            await connection.close()

    @asynccontextmanager
    async def session(self):
        async with self.sessions() as db:
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def invoke(self, operation, *args, **kwargs):
        async with self.session() as db:
            self.io_session = db
            return await getattr(service, operation)(db, *args, **kwargs)

    async def project_product(self, db, product_id, warehouse_id):
        evidence = {'kind': 'product', 'product_id': product_id, 'warehouse_id': warehouse_id,
                    'sku': 'SKU-A', 'currency': 'EUR', 'unit_cost': self.product_cost,
                    'fifo_revision': self.product_version}
        return evidence | {'source_hash': service.digest(evidence)}

    async def project_order(self, db, order_id):
        order = await db.get(ShopOrder, order_id)
        evidence = {'kind': 'order', 'order_id': order_id, 'shop_id': self.shop_id,
            'warehouse_id': self.warehouse_id, 'order_number': order.external_id, 'source_uuid': 'original-order',
            'currency': 'EUR', 'total_cost': '60', 'source_lines': [{'line_key': 'line-a', 'code': 'SKU-A',
                'ean': None, 'quantity': '3', 'kind': 'product', 'parent_uuid': None}],
            'lines': [{'line_key': 'line-a', 'code': 'SKU-A', 'quantity': '3', 'unit_cost': '20', 'total_cost': '60'}]}
        return evidence | {'source_hash': service.digest(evidence)}

    async def read_product(self, *args):
        self.assertFalse(self.io_session.in_transaction(), 'Network must not hold DB locks')
        self.reads += 1
        if self.read_hook:
            await self.read_hook(self.reads)
        return copy.deepcopy(self.remote)

    async def read_order(self, *args):
        self.assertFalse(self.io_session.in_transaction(), 'Network must not hold DB locks')
        return copy.deepcopy(self.order_remote)

    async def write_remote(self, shop_code, document, fingerprint):
        self.assertFalse(self.io_session.in_transaction(), 'Sending intent must commit before PUT')
        async with self.session() as db:
            row = await db.scalar(select(Publication).where(Publication.status == 'sending'))
            self.assertIsNotNone(row, 'Durable sending fence required')
        if self.write_error:
            raise self.write_error
        self.remote.update(copy.deepcopy(document['after']))

    def config(self, **changes):
        values = dict(shop_code='biketrek', warehouse_code='cost-central', expected_revision=0,
                      enabled=False, product_cost_enabled=True, order_cost_enabled=False, confirmed=True)
        return CostShopInput(**(values | changes))

    async def queue_product(self, *, automatic=False):
        await self.invoke('enqueue', CostRunInput(shop_code='biketrek', confirmed=True), automatic=automatic)
        await self.invoke('scan_batch', self.shop_id)
        async with self.session() as db:
            return await db.scalar(select(Publication.id).where(Publication.status == 'queued'))

    async def test_defaults_off_inheritance_and_server_gate(self):
        result = await self.invoke('options', 'biketrek')
        self.assertFalse(result['settings']['enabled'])
        self.assertFalse(result['settings']['product_cost_enabled'])
        await self.invoke('configure_warehouse', CostWarehouseInput(warehouse_code='cost-central',
            expected_revision=0, interval_seconds=600, batch_size=7, confirmed=True))
        result = await self.invoke('configure', self.config(interval_seconds=120))
        self.assertEqual(result['effective'], {'interval_seconds': 120, 'batch_size': 7})
        with patch.object(service, 'enabled', return_value=False), self.assertRaises(service.CostSyncError):
            await self.queue_product()
        service.source.write_once.assert_not_awaited()

    async def test_receipt_next_layer_reconciles_then_automatic_cache_skips_get(self):
        await self.invoke('configure', self.config(enabled=True))
        publication_id = await self.queue_product(automatic=True)
        await self.invoke('process_publication', publication_id)
        self.assertEqual((await self.invoke('get_publication', publication_id))['status'], 'verified')
        self.assertEqual(self.remote['values']['price_purchase'], '10')
        service.source.read_product.reset_mock()
        self.assertIsNone(await self.queue_product(automatic=True))
        service.source.read_product.assert_not_awaited()
        self.clock += timedelta(seconds=1)
        self.product_version, self.product_cost = 2, '40'
        publication_id = await self.queue_product(automatic=True)
        await self.invoke('process_publication', publication_id)
        self.assertEqual(self.remote['values']['price_purchase'], '40')

    async def test_manual_run_rechecks_external_cost_drift(self):
        await self.invoke('configure', self.config(enabled=True))
        publication_id = await self.queue_product(automatic=True)
        await self.invoke('process_publication', publication_id)
        self.clock += timedelta(seconds=1)
        self.remote['values']['price_purchase'] = '99'
        publication_id = await self.queue_product()
        self.assertIsNotNone(publication_id)
        await self.invoke('process_publication', publication_id)
        self.assertEqual(self.remote['values']['price_purchase'], '10')

    async def test_newer_resolved_mismatch_invalidates_older_verified_cache(self):
        await self.invoke('configure', self.config(enabled=True))
        publication_id = await self.queue_product(automatic=True)
        await self.invoke('process_publication', publication_id)
        self.clock += timedelta(seconds=1)
        async with self.session() as db:
            prior = await db.get(Publication, publication_id)
            db.add(Publication(id=str(uuid4()), shop_id=self.shop_id, warehouse_id=self.warehouse_id,
                kind='product', target_key=prior.target_key, subject='SKU-A', status='resolved', automatic=False,
                settings_revision=prior.settings_revision, target_fingerprint=prior.target_fingerprint,
                source_hash=prior.source_hash, source=prior.source, target=prior.target,
                created_at=self.clock, error='fifo_cost_resolved_without_match'))
        self.assertIsNotNone(await self.queue_product(automatic=True))

    async def test_changed_fifo_before_send_never_writes_stale_cost(self):
        await self.invoke('configure', self.config())
        publication_id = await self.queue_product()
        self.product_version = 2
        await self.invoke('process_publication', publication_id)
        result = await self.invoke('get_publication', publication_id)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error'], 'fifo_cost_source_changed')
        service.source.write_once.assert_not_awaited()

    async def test_second_read_closes_shared_parent_claim_gap(self):
        await self.invoke('configure', self.config())
        publication_id = await self.queue_product()
        async def change(count):
            if count == 2:
                self.remote['guard'] = 'b' * 64
        self.read_hook = change
        await self.invoke('process_publication', publication_id)
        result = await self.invoke('get_publication', publication_id)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error'], 'fifo_cost_remote_changed')
        service.source.write_once.assert_not_awaited()

    async def test_disable_during_final_get_prevents_put(self):
        await self.invoke('configure', self.config())
        publication_id = await self.queue_product()
        async def change(count):
            if count == 2:
                async with self.session() as db:
                    await service.configure(db, self.config(expected_revision=1, product_cost_enabled=False))
        self.read_hook = change
        await self.invoke('process_publication', publication_id)
        self.assertEqual((await self.invoke('get_publication', publication_id))['status'], 'failed')
        service.source.write_once.assert_not_awaited()

    async def test_ambiguous_put_fences_new_cost_and_restart_never_replays(self):
        await self.invoke('configure', self.config())
        publication_id = await self.queue_product()
        self.write_error = service.source.SourceError('test_timeout', uncertain=True)
        await self.invoke('process_publication', publication_id)
        self.assertEqual((await self.invoke('get_publication', publication_id))['status'], 'uncertain')
        self.product_version = 2
        self.assertIsNone(await self.queue_product())
        await self.invoke('recover')
        await self.invoke('process_publication', publication_id)
        self.assertEqual(service.source.write_once.await_count, 1)
        self.clock += timedelta(minutes=6)
        result = await self.invoke('resolve', publication_id, CostResolveInput(confirmed=True,
            original_request_settled=True, note='Original upstream request confirmed finished'))
        self.assertEqual(result['status'], 'resolved')
        self.assertIsNotNone(await self.queue_product())

    async def test_remote_mapping_id_replacement_is_blocked(self):
        await self.invoke('configure', self.config())
        publication_id = await self.queue_product()
        self.remote['identity']['variant_id'] = 999
        await self.invoke('process_publication', publication_id)
        result = await self.invoke('get_publication', publication_id)
        self.assertEqual(result['error'], 'fifo_cost_mapping_changed')
        service.source.write_once.assert_not_awaited()

    async def test_restart_after_intent_keeps_uncertain_fence(self):
        await self.invoke('configure', self.config())
        publication_id = await self.queue_product()
        self.write_error = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.invoke('process_publication', publication_id)
        self.assertEqual((await self.invoke('get_publication', publication_id))['status'], 'sending')
        await self.invoke('recover')
        self.assertEqual((await self.invoke('get_publication', publication_id))['status'], 'uncertain')
        await self.invoke('process_publication', publication_id)
        self.assertEqual(service.source.write_once.await_count, 1)

    async def test_retry_after_is_persisted_without_replaying_possible_put(self):
        from inventory_hub.stock_settings_models import StockShopSettings
        from inventory_hub.services import stock_publication
        await self.invoke('configure', self.config())
        publication_id = await self.queue_product()
        self.write_error = service.source.SourceError('rate_limited', 429, retry_after=3600, uncertain=True)
        with patch.object(stock_publication, 'target_fingerprint', return_value='f' * 64):
            await self.invoke('process_publication', publication_id)
        self.assertEqual((await self.invoke('get_publication', publication_id))['status'], 'uncertain')
        async with self.session() as db:
            policy = await db.get(Policy, self.shop_id)
            self.assertGreaterEqual(policy.retry_after_at, self.clock + timedelta(seconds=3600))
            shared = await db.get(StockShopSettings, self.shop_id)
            self.assertIsNotNone(shared.processing_retry_after_at)
        with self.assertRaises(service.CostSyncError) as error:
            await self.queue_product()
        self.assertEqual(error.exception.code, 'fifo_cost_retry_later')
        self.assertEqual(service.source.write_once.await_count, 1)

    async def test_recreated_local_mapping_invalidates_queued_target(self):
        await self.invoke('configure', self.config())
        publication_id = await self.queue_product()
        async with self.session() as db:
            mapping = await db.scalar(select(ShopProduct).where(ShopProduct.product_id == self.product_id))
            mapping.external_id = '999'
        await self.invoke('process_publication', publication_id)
        self.assertEqual((await self.invoke('get_publication', publication_id))['error'], 'fifo_cost_source_changed')
        service.source.write_once.assert_not_awaited()

    async def test_unicode_and_external_id_collision_fail_closed(self):
        async with self.session() as db:
            other = Product(sku='SKU-B', name='Other')
            db.add(other); await db.flush()
            db.add(ShopProduct(shop_id=self.shop_id, product_id=other.id, external_code='OTHER', external_id='91',
                variant_code='OTHER', parent_code='POS-xTrek', is_variant=True))
        async with self.session() as db:
            with self.assertRaises(service.CostSyncError) as error:
                await service.product_target(db, self.shop_id, self.product_id, 'SKU-A')
            self.assertEqual(error.exception.code, 'fifo_cost_mapping_ambiguous')

    async def test_activation_cutoff_uses_issue_date_not_original_creation(self):
        await self.invoke('configure', self.config(enabled=True, product_cost_enabled=False, order_cost_enabled=True))
        async with self.session() as db:
            for number, issued in (('OLD-CLOSED', self.clock - timedelta(days=1)),
                                   ('OLD-OPEN-NOW-ISSUED', self.clock + timedelta(seconds=1))):
                db.add(ShopOrder(shop_id=self.shop_id, external_id=number, order_date=self.clock - timedelta(days=10),
                    stock_state='issued', stock_warehouse_id=self.warehouse_id, stock_issued_at=issued))
        await self.invoke('enqueue', CostRunInput(shop_code='biketrek', confirmed=True), automatic=True)
        await self.invoke('scan_batch', self.shop_id)
        rows = (await self.invoke('history', 'biketrek'))['items']
        self.assertEqual([row['subject'] for row in rows], ['OLD-OPEN-NOW-ISSUED'])
        with self.assertRaises(service.CostSyncError):
            await self.invoke('configure', self.config(expected_revision=0))

    async def test_prepared_preview_send_is_idempotent_and_old_order_allowed(self):
        await self.invoke('configure', self.config(product_cost_enabled=False, order_cost_enabled=True))
        async with self.session() as db:
            order = ShopOrder(shop_id=self.shop_id, external_id='OLD-CLOSED', order_date=self.clock - timedelta(days=10),
                stock_state='issued', stock_warehouse_id=self.warehouse_id, stock_issued_at=self.clock - timedelta(days=5))
            db.add(order); await db.flush()
            order_id = order.id
        async with self.session() as db:
            evidence = await self.project_order(db, order_id)
        self.order_remote = {'kind': 'order', 'identity': {'uuid': 'original-order'},
            'source_lines': evidence['source_lines']}
        document = {'kind': 'order', 'identity': self.order_remote['identity'],
            'before': {'values': {'line-a': '10'}}, 'after': {'values': {'line-a': '20'}},
            'prices_with_vat_yn': False, 'selected': [{'line_key': 'line-a', 'code': 'SKU-A'}]}
        payload = CostOrderPreviewInput(shop_code='biketrek', order_number='OLD-CLOSED', confirmed=True, request_id=uuid4())
        with patch.object(service.source, 'prepare_order', return_value=document):
            first = await self.invoke('order_preview', payload)
            second = await self.invoke('order_preview', payload)
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(service.source.read_order.await_count, 1)
        self.assertEqual(first['remote_costs']['lines'][0]['desired'], '20')
        await self.invoke('send', first['id'])
        await self.invoke('send', first['id'])
        self.assertEqual(len((await self.invoke('history', 'biketrek'))['items']), 1)
        service.source.write_once.assert_not_awaited()
