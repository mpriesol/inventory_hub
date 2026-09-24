"""Regular publication durability and order gates in an isolated database."""
import asyncio
import copy
import os
import unittest
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit
from uuid import uuid4
import asyncpg
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool
from inventory_hub.db_models import MovementType, Product, Shop, Warehouse
from inventory_hub.db_models_ext import ShopProduct, StockBalance, StockMovement
from inventory_hub.order_stock_models import OrderStockPolicy
from inventory_hub.order_collection_models import OrderCollectionSettings, OrderInboxEntry
from inventory_hub.stock_settings_models import StockShopSettings
from inventory_hub.stock_sync_models import StockSyncSettings, StockSyncRun, StockSyncItem
from inventory_hub.stock_sync_types import ShopSyncInput, WarehouseSyncInput, SyncRunInput, SyncResolveInput
from inventory_hub.stock_publication_types import StockPublicationOpenHold
from inventory_hub.services import stock_sync as service, stock_publication
from inventory_hub.services import order_collection

TEST_URL = os.environ.get('CATALOG_TEST_DATABASE_URL','')


@unittest.skipUnless(TEST_URL, 'Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB')
class StockSyncDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed=urlsplit(TEST_URL)
        if parsed.hostname not in ('localhost','127.0.0.1') or not parsed.path.endswith('_catalog_test'):
            raise RuntimeError('Regular sync tests require isolated localhost *_catalog_test')
        self.schema='stock_sync_test_'+uuid4().hex
        connection=await asyncpg.connect(TEST_URL)
        root=Path(__file__).resolve().parents[2]/'infra'/'db-init'
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            await connection.execute((root/'001_schema.sql').read_text())
            await connection.execute("CREATE TYPE payment_status AS ENUM ('unpaid','partial','paid')")
            for name in ('002_invoice_management.sql','006_opening_stock.sql','007_order_stock.sql','008_order_collection.sql',
                         '009_stock_automation.sql','010_stock_publication.sql','011_fifo.sql','015_stock_sync.sql'):
                await connection.execute((root/name).read_text())
        finally:
            await connection.close()
        self.engine=create_async_engine(TEST_URL.replace('postgresql://','postgresql+asyncpg://',1),poolclass=NullPool,
                                        connect_args={'server_settings':{'search_path':self.schema}})
        self.sessions=async_sessionmaker(self.engine,expire_on_commit=False,autoflush=False)
        self.clock=service.now()
        self.remote={'identity':{'code':'SKU-A','parent_code':'xTrek','variant_code':'SKU-A','product_id':90,'variant_id':91},
                     'stock':'9','availability':'overíme','can_add_to_basket_yn':True}
        self.io_session=None
        self.patches=[patch.object(service,'now',return_value=self.clock),patch.object(service,'enabled',return_value=True),
            patch.object(order_collection,'load_shop',return_value={'upgates_api_base_url':'https://regular-test.invalid/api/v2',
                'upgates_login':'synthetic-test','upgates_api_key':'test-only'}),
            patch.object(service.source,'read',AsyncMock(side_effect=self.read_remote)),
            patch.object(service.source,'write_once',AsyncMock(side_effect=self.write_remote)),
            patch('inventory_hub.services.supplier_availability.project',AsyncMock(return_value={}))]
        for p in self.patches:p.start()
        async with self.session() as db:
            shop=await db.scalar(select(Shop).where(Shop.code=='biketrek'))
            self.shop_id=shop.id
            warehouse=Warehouse(code='regular-central',name='Regular central')
            product=Product(sku='SKU-A',name='SKU-A')
            db.add_all([warehouse,product]);await db.flush()
            self.warehouse_id,self.product_id=warehouse.id,product.id
            db.add(OrderStockPolicy(shop_id=shop.id,warehouse_id=warehouse.id,starts_at=self.clock-timedelta(days=30),
                revision=1,status_actions={},status_hash='a'*64,statuses=[]))
            db.add(StockShopSettings(shop_id=shop.id,revision=1,mode='reserve',overrides={},updated_at=self.clock,
                automation_starts_at=self.clock-timedelta(days=1),authorized_policy_revision=1,
                target_fingerprint=order_collection.target_fingerprint('biketrek')))
            db.add(OrderCollectionSettings(shop_id=shop.id,enabled=True,revision=1,
                target_fingerprint=order_collection.target_fingerprint('biketrek'),cursor_at=self.clock,
                reconcile_cursor_at=self.clock,last_completed_at=self.clock,next_poll_at=self.clock))
            db.add(ShopProduct(shop_id=shop.id,product_id=product.id,external_code='xTrek',parent_code='xTrek',variant_code='SKU-A',is_variant=True))
            db.add(StockBalance(product_id=product.id,warehouse_id=warehouse.id,qty_on_hand=7,qty_reserved=2,qty_quarantined=1,avg_cost=3,total_value=21))
            db.add(StockMovement(product_id=product.id,warehouse_id=warehouse.id,movement_type=MovementType.INITIAL,quantity=7,
                unit_cost=3,balance_after=7,avg_cost_after=3,idempotency_key=uuid4().hex))

    async def asyncTearDown(self):
        for p in reversed(self.patches):p.stop()
        await self.engine.dispose()
        c=await asyncpg.connect(TEST_URL)
        try:await c.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:await c.close()

    @asynccontextmanager
    async def session(self):
        async with self.sessions() as db:
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def invoke(self, operation,*args):
        async with self.session() as db:
            self.io_session=db
            return await getattr(service,operation)(db,*args)

    async def read_remote(self,*args):
        self.assertFalse(self.io_session.in_transaction(),'No locks across network')
        return copy.deepcopy(self.remote)

    async def write_remote(self,shop,target,desired,fingerprint):
        self.assertFalse(self.io_session.in_transaction(),'Intent must commit before write')
        async with self.session() as db:
            item=await db.scalar(select(StockSyncItem).where(StockSyncItem.status=='sending'))
            self.assertIsNotNone(item,'Durable target fence required')
        self.remote.update(desired)
        return {'acknowledged':True}

    def config(self,**changes):
        return ShopSyncInput(**dict(shop_code='biketrek',expected_revision=0,enabled=False,authorized=True,
            hub_is_stock_authority=True,external_stock_writers_disabled=True,orders_reconciled=True,confirmed=True,**changes))

    async def prepare(self):
        await self.invoke('configure',self.config())
        return await self.invoke('enqueue',SyncRunInput(shop_code='biketrek',confirmed=True,skus=['SKU-A']))

    async def test_off_defaults_and_authority_required(self):
        options=await self.invoke('options','biketrek')
        self.assertFalse(options['settings']['authorized']);self.assertFalse(options['settings']['enabled'])
        with self.assertRaises(service.SyncError):
            await self.invoke('enqueue',SyncRunInput(shop_code='biketrek',confirmed=True))
        with self.assertRaises(service.SyncError):
            await self.invoke('configure',ShopSyncInput(shop_code='biketrek',expected_revision=0,enabled=True,authorized=True,confirmed=True))
        service.source.write_once.assert_not_awaited()

    async def test_manual_write_uses_free_stock_and_preserves_ledger(self):
        row=await self.prepare()
        await self.invoke('process_run',row['id'])
        result=await self.invoke('get_run',row['id'])
        self.assertEqual(result['status'],'completed')
        self.assertEqual(self.remote['stock'],'4');self.assertEqual(self.remote['availability'],'SKLADOM')
        self.assertEqual(result['counts']['verified'],1)
        async with self.session() as db:
            self.assertEqual(len((await db.scalars(select(StockMovement))).all()),1)
            balance=await db.scalar(select(StockBalance).where(StockBalance.product_id==self.product_id))
            self.assertEqual(balance.qty_on_hand,7);self.assertEqual(balance.qty_reserved,2)
            holds=await db.scalar(text('SELECT count(*) FROM stock_publication_holds'))
            self.assertEqual(holds,0)

    async def test_settings_inherit_warehouse_and_shop_override(self):
        await self.invoke('configure_warehouse',WarehouseSyncInput(warehouse_code='regular-central',expected_revision=0,
                          interval_seconds=600,batch_size=10,max_order_age_seconds=1200,confirmed=True))
        options=await self.invoke('configure',self.config(interval_seconds=120))
        self.assertEqual(options['effective'],dict(interval_seconds=120,batch_size=10,max_order_age_seconds=1200))

    async def test_freshness_and_unprocessed_order_block_transfer(self):
        await self.invoke('configure',self.config())
        async with self.session() as db:
            collector=await db.get(OrderCollectionSettings,self.shop_id)
            collector.cursor_at=self.clock-timedelta(hours=1)
        with self.assertRaises(service.SyncError) as raised:
            await self.invoke('enqueue',SyncRunInput(shop_code='biketrek',confirmed=True))
        self.assertEqual(raised.exception.code,'stock_sync_orders_stale')
        async with self.session() as db:
            collector=await db.get(OrderCollectionSettings,self.shop_id);collector.cursor_at=self.clock
            db.add(OrderInboxEntry(shop_id=self.shop_id,source_uuid=str(uuid4()),order_number='TEST',created_at=self.clock,
                updated_at=self.clock,deleted=False,origin='web',status_id=1,observation_hash='b'*64,
                observed_at=self.clock,last_seen_at=self.clock))
        with self.assertRaises(service.SyncError) as raised:
            await self.invoke('enqueue',SyncRunInput(shop_code='biketrek',confirmed=True))
        self.assertEqual(raised.exception.code,'stock_sync_orders_pending')
        service.source.write_once.assert_not_awaited()

    async def test_ambiguous_write_fences_target_and_maintenance_until_resolved(self):
        row=await self.prepare()
        async def uncertain(*args):
            await self.write_remote(*args)
            raise service.source.SourceError('stock_publication_write_unconfirmed',uncertain=True)
        service.source.write_once.side_effect=uncertain
        await self.invoke('process_run',row['id'])
        result=await self.invoke('get_run',row['id'])
        self.assertEqual(result['status'],'uncertain')
        await self.invoke('recover')
        async with self.session() as db:
            with self.assertRaises(stock_publication.PublicationError) as raised:
                await stock_publication.open_hold(db,StockPublicationOpenHold(shop_code='biketrek',confirmed=True,
                                                    external_writers_paused=True,orders_reconciled=True))
            self.assertEqual(raised.exception.code,'stock_sync_inflight')
        second=await self.invoke('enqueue',SyncRunInput(shop_code='biketrek',confirmed=True,skus=['SKU-A']))
        await self.invoke('process_run',second['id'])
        self.assertEqual(service.source.write_once.await_count,1)
        resolved=await self.invoke('resolve',result['items'][0]['id'],SyncResolveInput(confirmed=True,external_requests_finished=True))
        self.assertEqual(resolved['status'],'completed');self.assertEqual(service.source.write_once.await_count,1)

    async def test_stock_changed_during_remote_read_is_reprojected_before_put(self):
        row=await self.prepare()
        first=True
        async def read(*args):
            nonlocal first
            observed=await self.read_remote(*args)
            if first:
                first=False
                async with self.session() as db:
                    balance=await db.scalar(select(StockBalance).where(StockBalance.product_id==self.product_id))
                    balance.qty_reserved=3
            return observed
        service.source.read.side_effect=read
        await self.invoke('process_run',row['id'])
        self.assertEqual(self.remote['stock'],'3')

    async def test_unknown_balance_is_skipped_never_published_as_zero(self):
        async with self.session() as db:
            p=Product(sku='UNKNOWN',name='Unknown');db.add(p);await db.flush()
            db.add(ShopProduct(shop_id=self.shop_id,product_id=p.id,external_code='UNKNOWN',is_variant=False))
        await self.invoke('configure',self.config())
        row=await self.invoke('enqueue',SyncRunInput(shop_code='biketrek',confirmed=True,skus=['UNKNOWN']))
        await self.invoke('process_run',row['id'])
        result=await self.invoke('get_run',row['id'])
        self.assertEqual(result['items'][0]['error'],'stock_projection_balance_missing')
        service.source.write_once.assert_not_awaited()

    async def test_cancellation_after_dispatch_is_durable_uncertainty(self):
        row=await self.prepare()
        service.source.write_once.side_effect=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.invoke('process_run',row['id'])
        await self.invoke('recover')
        await self.invoke('process_run',row['id'])
        result=await self.invoke('get_run',row['id'])
        self.assertEqual(result['items'][0]['status'],'uncertain')
        self.assertEqual(service.source.write_once.await_count,1)

    async def test_full_pass_advances_beyond_first_batch(self):
        async with self.session() as db:
            product=Product(sku='UNKNOWN',name='Unknown');db.add(product);await db.flush()
            db.add(ShopProduct(shop_id=self.shop_id,product_id=product.id,external_code='UNKNOWN',is_variant=False))
        await self.invoke('configure',self.config(batch_size=1))
        row=await self.invoke('enqueue',SyncRunInput(shop_code='biketrek',confirmed=True))
        await self.invoke('process_run',row['id'])
        first=await self.invoke('get_run',row['id'])
        self.assertEqual(first['status'],'queued');self.assertTrue(first['more_pending'])
        await self.invoke('process_run',row['id'])
        last=await self.invoke('get_run',row['id'])
        self.assertEqual(last['status'],'partial');self.assertFalse(last['more_pending'])
        self.assertEqual({item['sku'] for item in last['items']},{'SKU-A','UNKNOWN'})

    async def test_added_channel_requires_authority_reconfirmation(self):
        await self.invoke('configure',self.config())
        async with self.session() as db:
            second=await db.scalar(select(Shop).where(Shop.code=='xtrek'))
            db.add(ShopProduct(shop_id=second.id,product_id=self.product_id,external_code='SKU-A',is_variant=False))
        with self.assertRaises(service.SyncError) as raised:
            await self.invoke('enqueue',SyncRunInput(shop_code='biketrek',confirmed=True))
        self.assertEqual(raised.exception.code,'stock_sync_authority_changed')
        service.source.write_once.assert_not_awaited()

    async def test_fresh_supplier_label_then_expiry_and_no_supplier_quantity_in_own_stock(self):
        async with self.session() as db:
            balance=await db.scalar(select(StockBalance).where(StockBalance.product_id==self.product_id))
            balance.qty_reserved=6
        offer={self.product_id:{'available':True,'fresh':True,'label':'do 7 dní','quantity':'1000'}}
        with patch('inventory_hub.services.supplier_availability.project',AsyncMock(return_value=offer)):
            row=await self.prepare()
            await self.invoke('process_run',row['id'])
        self.assertEqual(self.remote['stock'],'0');self.assertEqual(self.remote['availability'],'do 7 dní')
        offer[self.product_id]['fresh']=False
        with patch('inventory_hub.services.supplier_availability.project',AsyncMock(return_value=offer)):
            row=await self.invoke('enqueue',SyncRunInput(shop_code='biketrek',confirmed=True,skus=['SKU-A']))
            await self.invoke('process_run',row['id'])
        self.assertEqual(self.remote['stock'],'0');self.assertEqual(self.remote['availability'],'overíme')
        self.assertIs(self.remote['can_add_to_basket_yn'],True)


    async def test_processing_cutoff_change_invalidates_previously_reconciled_authority(self):
        await self.invoke('configure',self.config())
        async with self.session() as db:
            processing=await db.get(StockShopSettings,self.shop_id)
            processing.automation_starts_at=self.clock
            db.add(OrderInboxEntry(shop_id=self.shop_id,source_uuid=str(uuid4()),order_number='BEFORE-RESET',
                created_at=self.clock-timedelta(hours=1),updated_at=self.clock,deleted=False,origin='web',status_id=1,
                observation_hash='c'*64,observed_at=self.clock,last_seen_at=self.clock))
        with self.assertRaises(service.SyncError) as raised:
            await self.invoke('enqueue',SyncRunInput(shop_code='biketrek',confirmed=True))
        self.assertEqual(raised.exception.code,'stock_sync_authority_changed')
        service.source.write_once.assert_not_awaited()
