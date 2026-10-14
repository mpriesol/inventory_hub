"""Regular publication durability and order gates in an isolated database."""
import asyncio
import copy
import os
import unittest
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlsplit
from uuid import uuid4
import asyncpg
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool
from inventory_hub.db_models import MovementType, Product, Shop, Warehouse
from inventory_hub.db_models_ext import ShopOrder, ShopProduct, StockBalance, StockMovement
from inventory_hub.order_stock_models import OrderStockPolicy
from inventory_hub.order_collection_models import OrderCollectionSettings, OrderInboxEntry
from inventory_hub.order_processing_models import OrderProcessingJob
from inventory_hub.stock_settings_models import StockShopSettings
from inventory_hub.stock_sync_models import StockSyncSettings, StockSyncRun, StockSyncItem
from inventory_hub.stock_delta_models import StockSyncCheckpoint
from inventory_hub.stock_publication_models import StockPublicationHold
from inventory_hub.stock_sync_types import ShopSyncInput, WarehouseSyncInput, SyncRunInput, SyncResolveInput, SyncBaselineInput
from inventory_hub.stock_publication_types import StockPublicationOpenHold
from inventory_hub.services import stock_sync as service, stock_publication, stock_delta
from inventory_hub.services import order_collection
from inventory_hub.services import order_processing, order_stock_source
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK
from test_order_stock import ACTIONS, STATUSES, raw_line, raw_order, raw_page

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
            await connection.execute((root/'019_stock_tracking.sql').read_text())
            await connection.execute("CREATE TYPE payment_status AS ENUM ('unpaid','partial','paid')")
            for name in ('002_invoice_management.sql','005_ai_content.sql','006_opening_stock.sql','007_order_stock.sql','008_order_collection.sql',
                         '009_stock_automation.sql','010_stock_publication.sql','011_fifo.sql','015_stock_sync.sql','017_stock_adjustments.sql',
                         '025_stock_native.sql','026_stock_delta.sql'):
                await connection.execute((root/name).read_text())
        finally:
            await connection.close()
        self.engine=create_async_engine(TEST_URL.replace('postgresql://','postgresql+asyncpg://',1),poolclass=NullPool,
                                        connect_args={'server_settings':{'search_path':self.schema}})
        self.sessions=async_sessionmaker(self.engine,expire_on_commit=False,autoflush=False)
        self.clock=service.now()
        self.remote={'identity':{'code':'SKU-A','parent_code':'xTrek','variant_code':'SKU-A','product_id':90,'variant_id':91},
                     'stock':'9','availability':'overíme','can_add_to_basket_yn':True}
        self.remote_identities={"SKU-A":copy.deepcopy(self.remote["identity"])}
        self.io_session=None
        self.patches=[patch.object(service,'now',return_value=self.clock),patch.object(service,'enabled',return_value=True),
            patch.object(order_collection,'load_shop',return_value={'upgates_api_base_url':'https://regular-test.invalid/api/v2',
                'upgates_login':'synthetic-test','upgates_api_key':'test-only'}),
            patch.object(service.source,'read',AsyncMock(side_effect=self.read_remote)),
            patch.object(service.source,'read_many',AsyncMock(side_effect=self.read_many_remote)),
            patch.object(service.source,'write_delta_once',AsyncMock(side_effect=self.write_remote)),
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
            from stock_tracking_fixture import confirmed_inventory
            db.add(confirmed_inventory(product.id, warehouse.id))
            db.add(StockMovement(product_id=product.id,warehouse_id=warehouse.id,movement_type=MovementType.INITIAL,quantity=7,
                unit_cost=3,balance_after=7,avg_cost_after=3,idempotency_key=uuid4().hex))
            self.hold_id=str(uuid4())
            db.add(StockPublicationHold(id=self.hold_id,shop_id=self.shop_id,warehouse_id=self.warehouse_id,
                active=False,assertions={'external_writers_paused':True,'orders_reconciled':True,'confirmed':True},
                created_at=self.clock-timedelta(days=1),closed_at=self.clock-timedelta(days=1)))
            await db.flush()
            db.add(self.make_checkpoint(product.id,'SKU-A',local=9))

    def make_checkpoint(self,product_id,sku,local=4,*,native=0,audit_due=False):
        return StockSyncCheckpoint(shop_id=self.shop_id,sku=sku,product_id=product_id,warehouse_id=self.warehouse_id,
            target_fingerprint=order_collection.target_fingerprint('biketrek'),
            target=copy.deepcopy(self.remote_identities[sku]),local_quantity=local,native_quantity=native,
            metadata_values={'availability':'SKLADOM','can_add_to_basket_yn':True},hold_id=self.hold_id,
            baseline_at=self.clock-timedelta(days=1),updated_at=self.clock,
            last_audit_at=self.clock-timedelta(hours=7) if audit_due else self.clock)

    async def checkpoint(self,local=4,*,native=0,audit_due=False,metadata=None):
        async with self.session() as db:
            row=await db.get(StockSyncCheckpoint,(self.shop_id,'SKU-A'))
            row.local_quantity,row.native_quantity=local,native
            row.last_audit_at=self.clock-timedelta(hours=7) if audit_due else self.clock
            if metadata is not None:row.metadata_values=metadata

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
            handler=stock_delta.baseline if operation=='baseline' else getattr(service,operation)
            return await handler(db,*args)

    async def read_remote(self,*args):
        self.assertFalse(self.io_session.in_transaction(),'No locks across network')
        return copy.deepcopy(self.remote)

    async def read_many_remote(self,shop,targets,fingerprint):
        self.assertFalse(self.io_session.in_transaction(),'No locks across batched network reads')
        result={}
        for index,target in enumerate(targets):
            observation=copy.deepcopy(self.remote)
            observation['identity']=copy.deepcopy(self.remote_identities[target['code']])
            result[target['code']]={'observation':observation,'observed_at':self.clock,
                                   'observed_monotonic':service.monotonic()}
        return result

    async def seed_batch_products(self,codes,variant=True):
        from stock_tracking_fixture import confirmed_inventory
        async with self.session() as db:
            for code in codes:
                product=Product(sku=code,name=code);db.add(product);await db.flush()
                self.remote_identities[code]={'code':code,'parent_code':'xTrek' if variant else code,
                    'variant_code':code if variant else None,'product_id':90 if variant else 1000+len([v for v in self.remote_identities.values() if v['variant_id'] is None]),
                    'variant_id':91+len([v for v in self.remote_identities.values() if v['variant_id'] is not None]) if variant else None}
                db.add(ShopProduct(shop_id=self.shop_id,product_id=product.id,is_variant=variant,
                    external_code='xTrek' if variant else code,parent_code='xTrek' if variant else None,
                    variant_code=code if variant else None))
                db.add(StockBalance(product_id=product.id,warehouse_id=self.warehouse_id,
                    qty_on_hand=4,qty_reserved=0,qty_quarantined=0,avg_cost=1,total_value=4))
                db.add(confirmed_inventory(product.id,self.warehouse_id))
                db.add(StockMovement(product_id=product.id,warehouse_id=self.warehouse_id,
                    movement_type=MovementType.INITIAL,quantity=4,unit_cost=1,balance_after=4,
                    avg_cost_after=1,idempotency_key=uuid4().hex))
                db.add(self.make_checkpoint(product.id,code,audit_due=True))

    async def write_remote(self,shop,target,desired,fingerprint):
        self.assertFalse(self.io_session.in_transaction(),'Intent must commit before write')
        async with self.session() as db:
            item=await db.scalar(select(StockSyncItem).where(StockSyncItem.status=='sending'))
            self.assertIsNotNone(item,'Durable target fence required')
        self.assertNotIn('stock',desired,'Regular publication must never send an absolute stock field')
        self.assertIn('stock_increment',desired)
        self.remote['stock']=str(int(Decimal(self.remote['stock'])+Decimal(str(desired['stock_increment']))))
        self.remote.update({key:value for key,value in desired.items() if key!='stock_increment'})
        return {'acknowledged':True}

    def config(self,**changes):
        return ShopSyncInput(**dict(shop_code='biketrek',expected_revision=0,enabled=False,authorized=True,
            hub_is_stock_authority=True,external_stock_writers_disabled=True,native_order_stock_enabled=True,orders_reconciled=True,confirmed=True,**changes))

    async def prepare(self):
        await self.invoke('configure',self.config())
        return await self.invoke('enqueue',SyncRunInput(shop_code='biketrek',confirmed=True,skus=['SKU-A']))

    async def processed_order(self, quantity='8', status_id=1, shop_code='biketrek', previous=None):
        """Use the actual processor/ledger; only the external detail read is synthetic."""
        statuses=order_stock_source._statuses(STATUSES)
        if previous is None:
            raw=raw_order(raw_line(quantity=quantity,product_id=90 if shop_code=='biketrek' else 190,
                                   option_set_id=91 if shop_code=='biketrek' else None),order_number='WAIT-'+uuid4().hex[:12],
                          creation_time=self.clock.isoformat(),last_update_time=self.clock.isoformat(),status_id=status_id)
            source,source_hash=order_stock_source._order(raw_page(raw),raw['order_number'])
            async with self.session() as db:
                shop=await db.scalar(select(Shop).where(Shop.code==shop_code))
                policy=await db.get(OrderStockPolicy,shop.id)
                policy.status_actions,policy.status_hash,policy.statuses=dict(ACTIONS),statuses['status_hash'],statuses['statuses']
                if status_id==8:
                    config=await db.get(StockShopSettings,shop.id)
                    config.mode,config.issue_starts_at='fulfill',self.clock-timedelta(hours=1)
                entry=OrderInboxEntry(shop_id=shop.id,source_uuid=source['uuid'],order_number=source['order_number'],
                    created_at=self.clock,updated_at=self.clock,deleted=False,origin=source['origin'],status_id=status_id,
                    observation_hash=order_stock_source._hash(source),observed_at=self.clock,last_seen_at=self.clock)
                db.add(entry);await db.flush()
                data={'shop_id':shop.id,'entry_id':entry.id,'source':source,'source_hash':source_hash,'shop_code':shop_code}
            if shop_code=='biketrek' and status_id!=2:
                self.remote['stock']=str(int(Decimal(self.remote['stock'])-Decimal(quantity)))
        else:
            data=previous
        async with self.session() as db:
            await order_processing.enqueue(db,data['shop_id'],force=True)
        async with self.session() as db:
            job=await db.scalar(select(OrderProcessingJob).where(OrderProcessingJob.inbox_id==data['entry_id']))
            plan=await order_processing.start_job(db,job.id)
            self.assertIsNotNone(plan)
            data['job_id']=job.id
        async with self.session() as db:
            await order_processing.finish_job(db,plan,{'order':copy.deepcopy(data['source']),
                'source_hash':data['source_hash'],**statuses})
            job=await db.get(OrderProcessingJob,data['job_id'])
            data['result']=copy.deepcopy(job.result)
        return data

    async def assert_order_blocked(self):
        with self.assertRaises(service.SyncError) as raised:
            await self.invoke('enqueue',SyncRunInput(shop_code='biketrek',confirmed=True,skus=['SKU-A']))
        self.assertEqual(raised.exception.code,'stock_sync_orders_pending')
        service.source.write_delta_once.assert_not_awaited()

    async def test_accounted_backorder_publishes_zero_and_supplier_availability(self):
        order=await self.processed_order()
        row=await self.prepare()
        offer={self.product_id:{'available':True,'fresh':True,'label':'do 2–4 dní'}}
        with patch('inventory_hub.services.supplier_availability.project',AsyncMock(return_value=offer)):
            await self.invoke('process_run',row['id'])
        result=await self.invoke('get_run',row['id'])
        self.assertEqual(result['status'],'completed')
        self.assertEqual((self.remote['stock'],self.remote['availability']),('0','do 2–4 dní'))
        async with self.session() as db:
            job=await db.get(OrderProcessingJob,order['job_id'])
            self.assertEqual((job.status,job.error),('retry','order_processing_backorder'))
            self.assertEqual(len((await db.scalars(select(StockMovement))).all()),1)
            balance=await db.scalar(select(StockBalance).where(StockBalance.product_id==self.product_id))
            self.assertEqual((balance.qty_on_hand,balance.qty_reserved),(7,6))

    async def test_receipt_and_released_units_require_backorder_reallocation(self):
        order=await self.processed_order()
        await self.invoke('configure',self.config())
        async with self.session() as db:
            balance=await db.scalar(select(StockBalance).where(StockBalance.product_id==self.product_id))
            balance.qty_on_hand+=2;balance.total_value+=6
        await self.assert_order_blocked()
        order=await self.processed_order(previous=order)
        self.assertNotIn('stock_sync_orders_pending',(await self.invoke('options','biketrek'))['blockers'])
        async with self.session() as db:
            balance=await db.scalar(select(StockBalance).where(StockBalance.product_id==self.product_id))
            balance.qty_reserved-=2
        await self.assert_order_blocked()
        await self.processed_order(previous=order)
        self.assertNotIn('stock_sync_orders_pending',(await self.invoke('options','biketrek'))['blockers'])

    async def test_backorder_proof_rejects_stale_or_mismatched_evidence(self):
        order=await self.processed_order()
        await self.invoke('configure',self.config())
        cases=[(OrderInboxEntry,order['entry_id'],'observation_hash','c'*64),
               (OrderInboxEntry,order['entry_id'],'review_reason','order_collection_identity_conflict'),
               (OrderInboxEntry,order['entry_id'],'deleted',True),
               (OrderProcessingJob,order['job_id'],'order_number','OTHER'),
               (OrderProcessingJob,order['job_id'],'source_hash','d'*64),
               (OrderProcessingJob,order['job_id'],'configuration_hash','e'*64),
               (OrderProcessingJob,order['job_id'],'result',{**order['result'],'revision':999}),
               (ShopOrder,order['result']['order_id'],'stock_source_uuid',str(uuid4())),
               (ShopOrder,order['result']['order_id'],'stock_source_hash','f'*64),
               (ShopOrder,order['result']['order_id'],'stock_revision',999)]
        async with self.session() as db:
            other=Warehouse(code='other-proof',name='Other proof');db.add(other);await db.flush()
            cases.append((ShopOrder,order['result']['order_id'],'stock_warehouse_id',other.id))
        for model,identifier,field,value in cases:
            with self.subTest(field=field,model=model.__name__):
                async with self.session() as db:
                    item=await db.get(model,identifier);original=copy.deepcopy(getattr(item,field));setattr(item,field,value)
                await self.assert_order_blocked()
                async with self.session() as db:
                    setattr(await db.get(model,identifier),field,original)
        self.assertNotIn('stock_sync_orders_pending',(await self.invoke('options','biketrek'))['blockers'])

    async def test_insufficient_issue_and_failed_recheck_remain_blocking(self):
        order=await self.processed_order(status_id=8)
        await self.invoke('configure',self.config())
        async with self.session() as db:
            job=await db.get(OrderProcessingJob,order['job_id'])
            self.assertEqual((job.status,job.error),('retry','order_stock_insufficient_stock'))
        await self.assert_order_blocked()
        async with self.session() as db:
            job=await db.get(OrderProcessingJob,order['job_id'])
            job.error='order_processing_source_unavailable'
        await self.assert_order_blocked()

    async def test_backorder_rechecks_current_identity_without_rewriting_saved_snapshot(self):
        order=await self.processed_order()
        await self.invoke('configure',self.config())
        async with self.session() as db:
            saved=copy.deepcopy((await db.get(ShopOrder,order['result']['order_id'])).stock_snapshot)
            db.add(Product(sku='sku-a',name='Conflicting case identity'))
        await self.assert_order_blocked()
        async with self.session() as db:
            self.assertEqual((await db.get(ShopOrder,order['result']['order_id'])).stock_snapshot,saved)

    async def test_safe_backorder_cannot_hide_another_unprocessed_order(self):
        await self.processed_order()
        await self.invoke('configure',self.config())
        async with self.session() as db:
            db.add(OrderInboxEntry(shop_id=self.shop_id,source_uuid=str(uuid4()),order_number='NEW-UNPROCESSED',
                created_at=self.clock,updated_at=self.clock,deleted=False,origin='web',status_id=1,
                observation_hash='b'*64,observed_at=self.clock,last_seen_at=self.clock))
        await self.assert_order_blocked()

    async def test_backorder_on_shared_peer_allows_sync_but_new_peer_header_blocks_it(self):
        async with self.session() as db:
            shop=await db.scalar(select(Shop).where(Shop.code=='xtrek'))
            fingerprint=order_collection.target_fingerprint(shop.code)
            db.add(OrderStockPolicy(shop_id=shop.id,warehouse_id=self.warehouse_id,
                starts_at=self.clock-timedelta(days=30),revision=1,status_actions={},status_hash='a'*64,statuses=[]))
            db.add(StockShopSettings(shop_id=shop.id,revision=1,mode='reserve',overrides={},updated_at=self.clock,
                automation_starts_at=self.clock-timedelta(days=1),authorized_policy_revision=1,
                target_fingerprint=fingerprint))
            db.add(OrderCollectionSettings(shop_id=shop.id,enabled=True,revision=1,target_fingerprint=fingerprint,
                cursor_at=self.clock,reconcile_cursor_at=self.clock,last_completed_at=self.clock,next_poll_at=self.clock))
            db.add(ShopProduct(shop_id=shop.id,product_id=self.product_id,external_id='190',external_code='SKU-A',is_variant=False))
        order=await self.processed_order(shop_code='xtrek')
        row=await self.prepare()
        await self.invoke('process_run',row['id'])
        self.assertEqual((await self.invoke('get_run',row['id']))['status'],'completed')
        self.assertEqual(self.remote['stock'],'0')
        service.source.write_delta_once.reset_mock()
        async with self.session() as db:
            entry=await db.get(OrderInboxEntry,order['entry_id'])
            entry.observation_hash='c'*64
        await self.assert_order_blocked()

    async def test_backorder_rechecked_after_batch_read_before_stock_write(self):
        order=await self.processed_order()
        row=await self.prepare()
        async def changed_header(*args):
            observation=await self.read_many_remote(*args)
            async with self.session() as db:
                entry=await db.get(OrderInboxEntry,order['entry_id'])
                entry.observation_hash='c'*64
            return observation
        service.source.read_many.side_effect=changed_header
        await self.invoke('process_run',row['id'])
        result=await self.invoke('get_run',row['id'])
        self.assertEqual(result['items'][0]['error'],'stock_sync_orders_pending')
        service.source.write_delta_once.assert_not_awaited()

    async def test_backorder_identity_change_during_remote_read_blocks_stock_write(self):
        await self.processed_order()
        row=await self.prepare()
        async def changed_identity(*args):
            observation=await self.read_many_remote(*args)
            async with self.session() as db:
                await db.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':IDENTITY_WRITE_LOCK})
                db.add(Product(sku='sku-a',name='Identity changed during read'))
            return observation
        service.source.read_many.side_effect=changed_identity
        await self.invoke('process_run',row['id'])
        result=await self.invoke('get_run',row['id'])
        self.assertEqual(result['items'][0]['error'],'stock_sync_orders_pending')
        service.source.write_delta_once.assert_not_awaited()

    async def test_identity_lock_covers_both_projections_and_releases_for_network(self):
        await self.processed_order()
        row=await self.prepare()
        projections=[]
        network=[]
        original=service.desired
        async def available():
            async with self.sessions() as other:
                return await other.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'),{'key':IDENTITY_WRITE_LOCK})
        async def projected(*args):
            self.assertFalse(await available(),'Identity changes must wait through proof and projection')
            projections.append(True)
            return await original(*args)
        async def read(*args):
            self.assertTrue(await available(),'Identity lock must be released before remote reads')
            network.append('read')
            return await self.read_remote(*args)
        async def read_many(*args):
            self.assertTrue(await available(),'Identity lock must be released before batch reads')
            network.append('batch')
            return await self.read_many_remote(*args)
        async def write(*args):
            self.assertTrue(await available(),'Identity lock must be released before remote writes')
            network.append('write')
            return await self.write_remote(*args)
        service.source.read.side_effect=read
        service.source.read_many.side_effect=read_many
        service.source.write_delta_once.side_effect=write
        with patch.object(service,'desired',side_effect=projected):
            await self.invoke('process_run',row['id'])
        self.assertGreaterEqual(len(projections),2)
        self.assertEqual(network,['batch','write'])
        self.assertEqual((await self.invoke('get_run',row['id']))['status'],'completed')

    async def test_off_defaults_and_authority_required(self):
        options=await self.invoke('options','biketrek')
        self.assertFalse(options['settings']['authorized']);self.assertFalse(options['settings']['enabled'])
        with self.assertRaises(service.SyncError):
            await self.invoke('enqueue',SyncRunInput(shop_code='biketrek',confirmed=True))
        with self.assertRaises(service.SyncError):
            await self.invoke('configure',ShopSyncInput(shop_code='biketrek',expected_revision=0,enabled=True,authorized=True,confirmed=True))
        service.source.write_delta_once.assert_not_awaited()

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
            self.assertEqual(holds,1,'Synchronization must not create an additional maintenance hold')

    async def test_twenty_unchanged_leaves_use_one_batch_get_without_single_reads_or_puts(self):
        await self.checkpoint(local=4,audit_due=True)
        variants=['SKU-A',*[f'VARIANT-{index:02d}' for index in range(19)]]
        simples=[f'SIMPLE-{index:02d}' for index in range(20)]
        await self.seed_batch_products(variants[1:])
        await self.seed_batch_products(simples,variant=False)
        await self.invoke('configure',self.config())
        async with self.session() as db:
            before={table:(await db.execute(text(f'SELECT * FROM {table} ORDER BY id'))).mappings().all()
                    for table in ('stock_balances','stock_movements')}
        async def real_batch(*args):
            self.assertFalse(self.io_session.in_transaction())
            return await asyncio.to_thread(service.source._read_many,*args)
        def response(client,method,path,*,params):
            self.assertEqual(method,'get')
            variant=path=='products/variants'
            codes=params['variant_codes' if variant else 'codes'].split(';')
            rows=[{'code':code,'product_id':self.remote_identities[code]['product_id'],'stock':4,
                   'availability':'SKLADOM','can_add_to_basket_yn':True,
                   **({'product_code':'xTrek','variant_id':self.remote_identities[code]['variant_id']} if variant else
                      {'variants_exists_yn':False,'variants':[],'set_yn':False})}
                  for index,code in enumerate(codes)]
            return {'current_page':1,'current_page_items':len(rows),'number_of_items':len(rows),
                    'number_of_pages':1,'variants' if variant else 'products':rows}
        service.source.read_many.side_effect=real_batch
        with patch.object(service.source.transport,'_client',return_value=Mock()), \
             patch.object(service.source.transport,'_request',side_effect=response) as request:
            for codes in (variants,simples):
                request.reset_mock()
                run=await self.invoke('enqueue',SyncRunInput(shop_code='biketrek',confirmed=True,skus=codes))
                await self.invoke('process_run',run['id'])
                result=await self.invoke('get_run',run['id'])
                self.assertEqual((result['status'],result['counts']['verified']),('completed',20))
                request.assert_called_once()
        service.source.read.assert_not_awaited()
        service.source.write_delta_once.assert_not_awaited()
        async with self.session() as db:
            after={table:(await db.execute(text(f'SELECT * FROM {table} ORDER BY id'))).mappings().all()
                   for table in before}
        self.assertEqual(after,before)

    async def test_batch_match_rechecks_reservation_and_changed_leaf_keeps_fresh_write_protocol(self):
        await self.seed_batch_products(['SKU-B'])
        self.remote.update(stock='4',availability='SKLADOM')
        await self.checkpoint(local=4,audit_due=True)
        await self.invoke('configure',self.config())
        run=await self.invoke('enqueue',SyncRunInput(shop_code='biketrek',confirmed=True,skus=['SKU-A','SKU-B']))
        async def reserved_during_batch(*args):
            result=await self.read_many_remote(*args)
            async with self.session() as db:
                balance=await db.scalar(select(StockBalance).where(StockBalance.product_id==self.product_id))
                balance.qty_reserved=3
            return result
        service.source.read_many.side_effect=reserved_during_batch
        await self.invoke('process_run',run['id'])
        result=await self.invoke('get_run',run['id'])
        self.assertEqual((result['status'],result['counts']['verified']),('completed',2))
        self.assertEqual(self.remote['stock'],'3')
        service.source.read.assert_not_awaited()
        service.source.write_delta_once.assert_awaited_once()
        rows={item['sku']:item for item in result['items']}
        self.assertEqual(rows['SKU-A']['desired']['stock_increment'],'-1')
        self.assertEqual(rows['SKU-B']['desired']['stock_increment'],'0')
        self.assertIsNone(rows['SKU-B']['attempt_started_at'])

    async def test_expired_or_future_batch_proof_uses_fresh_read_without_write(self):
        self.remote.update(stock='4',availability='SKLADOM')
        await self.checkpoint(local=4,audit_due=True)
        await self.invoke('configure',self.config())
        for offset in (-31,60):
            await self.checkpoint(local=4,audit_due=True)
            run=await self.invoke('enqueue',SyncRunInput(shop_code='biketrek',confirmed=True,skus=['SKU-A']))
            async def aged(*args):
                result=await self.read_many_remote(*args)
                result['SKU-A']['observed_monotonic']+=offset
                return result
            service.source.read_many.side_effect=aged
            service.source.read.reset_mock()
            await self.invoke('process_run',run['id'])
            self.assertEqual((await self.invoke('get_run',run['id']))['status'],'completed')
            service.source.read.assert_awaited_once()
        service.source.write_delta_once.assert_not_awaited()

    async def test_batch_matching_value_cannot_skip_new_order_blocker(self):
        order=await self.processed_order()
        self.remote.update(stock='0',availability='overíme')
        await self.checkpoint(local=0,native=-8,audit_due=True,metadata={'availability':'overíme','can_add_to_basket_yn':True})
        run=await self.prepare()
        async def header_changed(*args):
            result=await self.read_many_remote(*args)
            async with self.session() as db:
                (await db.get(OrderInboxEntry,order['entry_id'])).observation_hash='c'*64
            return result
        service.source.read_many.side_effect=header_changed
        await self.invoke('process_run',run['id'])
        result=await self.invoke('get_run',run['id'])
        self.assertEqual(result['items'][0]['error'],'stock_sync_orders_pending')
        service.source.read.assert_not_awaited()
        service.source.write_delta_once.assert_not_awaited()

    async def test_batch_matching_value_cannot_skip_policy_revision_change(self):
        self.remote.update(stock='4',availability='SKLADOM')
        await self.checkpoint(local=4,audit_due=True)
        run=await self.prepare()
        async def changed_settings(*args):
            result=await self.read_many_remote(*args)
            async with self.session() as db:
                (await db.get(StockSyncSettings,self.shop_id)).revision+=1
            return result
        service.source.read_many.side_effect=changed_settings
        await self.invoke('process_run',run['id'])
        result=await self.invoke('get_run',run['id'])
        self.assertEqual(result['items'][0]['error'],'stock_sync_settings_changed')
        service.source.read.assert_not_awaited()
        service.source.write_delta_once.assert_not_awaited()

    async def test_batch_matching_value_cannot_skip_mapping_change(self):
        self.remote.update(stock='4',availability='SKLADOM')
        await self.checkpoint(local=4,audit_due=True)
        run=await self.prepare()
        async def mapped_elsewhere(*args):
            result=await self.read_many_remote(*args)
            async with self.session() as db:
                await db.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':IDENTITY_WRITE_LOCK})
                mapping=await db.scalar(select(ShopProduct).where(ShopProduct.product_id==self.product_id))
                mapping.parent_code=mapping.external_code='OTHER-PARENT'
            return result
        service.source.read_many.side_effect=mapped_elsewhere
        await self.invoke('process_run',run['id'])
        result=await self.invoke('get_run',run['id'])
        self.assertEqual(result['items'][0]['error'],'stock_sync_baseline_changed')
        service.source.read.assert_not_awaited()
        service.source.write_delta_once.assert_not_awaited()

    async def test_failed_batch_never_fans_out_to_newly_eligible_earlier_item(self):
        from stock_tracking_fixture import confirmed_inventory
        await self.invoke('configure',self.config())
        for index,error in enumerate((service.source.SourceError('stock_publication_invalid_response'),
                                     service.source.SourceError('stock_publication_rate_limited',429,retry_after=420))):
            code=f'A-UNOPENED-{index}'
            async with self.session() as db:
                product=Product(sku=code,name=code);db.add(product);await db.flush()
                product_id=product.id
                db.add(ShopProduct(shop_id=self.shop_id,product_id=product_id,external_code=code,is_variant=False))
            run=await self.invoke('enqueue',SyncRunInput(shop_code='biketrek',confirmed=True,skus=[code,'SKU-A']))
            async def opened_during_failed_batch(*args):
                self.assertFalse(self.io_session.in_transaction())
                self.assertEqual([target['code'] for target in args[1]],['SKU-A'])
                async with self.session() as db:
                    db.add(StockBalance(product_id=product_id,warehouse_id=self.warehouse_id,
                        qty_on_hand=0,qty_reserved=0,qty_quarantined=0,avg_cost=0,total_value=0))
                    db.add(confirmed_inventory(product_id,self.warehouse_id))
                raise error
            service.source.read_many.side_effect=opened_during_failed_batch
            await self.invoke('process_run',run['id'])
            result=await self.invoke('get_run',run['id'])
            self.assertEqual(result['counts']['skipped'],2)
            service.source.read.assert_not_awaited()
            service.source.write_delta_once.assert_not_awaited()
        async with self.session() as db:
            policy=await db.get(StockSyncSettings,self.shop_id)
            processing=await db.get(StockShopSettings,self.shop_id)
            self.assertEqual(policy.retry_after_at,self.clock+timedelta(seconds=420))
            self.assertIsNotNone(processing.processing_retry_after_at)

    async def test_batch_429_is_remembered_even_if_all_targets_become_ineligible(self):
        run=await self.prepare()
        async def disappeared(*args):
            async with self.session() as db:
                (await db.get(Product,self.product_id)).is_active=False
            raise service.source.SourceError('stock_publication_rate_limited',429,retry_after=480)
        service.source.read_many.side_effect=disappeared
        await self.invoke('process_run',run['id'])
        async with self.session() as db:
            policy=await db.get(StockSyncSettings,self.shop_id)
            self.assertEqual(policy.retry_after_at,self.clock+timedelta(seconds=480))
        service.source.read.assert_not_awaited()
        service.source.write_delta_once.assert_not_awaited()

    async def test_unexpected_prefetch_projection_error_does_not_strand_other_items(self):
        await self.seed_batch_products(['SKU-B'])
        self.remote.update(stock='4',availability='SKLADOM')
        await self.checkpoint(local=4,audit_due=True)
        await self.invoke('configure',self.config())
        run=await self.invoke('enqueue',SyncRunInput(shop_code='biketrek',confirmed=True,skus=['SKU-A','SKU-B']))
        original=service.desired
        async def broken_one(db,shop,sku):
            if sku=='SKU-A':
                raise RuntimeError('Synthetic per-item failure')
            return await original(db,shop,sku)
        with patch.object(service,'desired',side_effect=broken_one):
            await self.invoke('process_run',run['id'])
        result=await self.invoke('get_run',run['id'])
        self.assertEqual((result['status'],result['counts']['verified'],result['counts']['failed']),('partial',1,1))
        self.assertEqual(result['items'][0]['error'],'stock_sync_worker_error')
        service.source.read.assert_not_awaited()
        service.source.write_delta_once.assert_not_awaited()

    async def test_native_order_between_read_and_write_is_preserved_by_delta(self):
        """A fresh native order cannot be overwritten by a metadata publication."""
        async with self.session() as db:
            balance=await db.scalar(select(StockBalance).where(StockBalance.product_id==self.product_id))
            balance.qty_reserved=0
        order=await self.processed_order(quantity='2')
        self.remote.update(stock='4',availability='overíme')
        await self.checkpoint(local=4,native=-2,
            metadata={'availability':'overíme','can_add_to_basket_yn':True})
        row=await self.prepare()
        async with self.session() as db:
            self.assertEqual((await db.get(OrderProcessingJob,order['job_id'])).status,'completed')
            tables=('stock_balances','stock_movements','reservations','shop_orders','shop_order_items')
            before={table:(await db.execute(text(f'SELECT * FROM {table} ORDER BY id'))).mappings().all()
                    for table in tables}
        events=[]
        async def observed(*args):
            value=await self.read_many_remote(*args)
            events.append(('read',self.remote['stock']))
            return value
        async def concurrent_native_order(shop,target,payload,fingerprint):
            self.assertEqual(self.remote['stock'],'4')
            self.remote['stock']='3'
            events.append(('native_order','4','3'))
            events.append(('delta',str(payload['stock_increment'])))
            return await self.write_remote(shop,target,payload,fingerprint)
        service.source.read_many.side_effect=observed
        service.source.write_delta_once.side_effect=concurrent_native_order
        await self.invoke('process_run',row['id'])
        result=await self.invoke('get_run',row['id'])
        self.assertEqual(events,[('read','4'),('native_order','4','3'),('delta','0')])
        self.assertEqual((result['status'],result['counts']['verified']),('completed',1))
        self.assertEqual(self.remote['stock'],'3','The native deduction must survive Hub publication')
        service.source.write_delta_once.assert_awaited_once()
        service.source.read.assert_not_awaited()
        async with self.session() as db:
            after={table:(await db.execute(text(f'SELECT * FROM {table} ORDER BY id'))).mappings().all()
                   for table in tables}
            self.assertEqual(after,before,'Publication must not rewrite the local ledger or order state')

    async def test_fresh_unchanged_checkpoint_needs_no_product_api_calls(self):
        self.remote.update(stock='4',availability='SKLADOM')
        await self.checkpoint(local=4)
        row=await self.prepare()
        await self.invoke('process_run',row['id'])
        result=await self.invoke('get_run',row['id'])
        self.assertEqual((result['status'],result['counts']['verified']),('completed',1))
        service.source.read_many.assert_not_awaited()
        service.source.read.assert_not_awaited()
        service.source.write_delta_once.assert_not_awaited()

    async def test_native_order_deduction_is_not_sent_back_to_its_own_shop(self):
        async with self.session() as db:
            balance=await db.scalar(select(StockBalance).where(StockBalance.product_id==self.product_id))
            balance.qty_on_hand,balance.qty_reserved,balance.total_value=5,0,15
        self.remote.update(stock='4',availability='SKLADOM')
        await self.checkpoint(local=4)
        await self.processed_order(quantity='2')
        row=await self.prepare()
        await self.invoke('process_run',row['id'])
        self.assertEqual((await self.invoke('get_run',row['id']))['status'],'completed')
        self.assertEqual(self.remote['stock'],'2')
        service.source.write_delta_once.assert_not_awaited()
        async with self.session() as db:
            point=await db.get(StockSyncCheckpoint,(self.shop_id,'SKU-A'))
            self.assertEqual((point.local_quantity,point.native_quantity),(2,-2))

    async def test_backorder_offset_uses_full_native_demand_not_only_allocated_quantity(self):
        async with self.session() as db:
            balance=await db.scalar(select(StockBalance).where(StockBalance.product_id==self.product_id))
            balance.qty_on_hand,balance.qty_reserved,balance.total_value=5,0,15
        self.remote.update(stock='4',availability='SKLADOM')
        await self.checkpoint(local=4)
        await self.processed_order(quantity='8')
        self.assertEqual(self.remote['stock'],'-4')
        row=await self.prepare()
        await self.invoke('process_run',row['id'])
        self.assertEqual((await self.invoke('get_run',row['id']))['status'],'completed')
        self.assertEqual(self.remote['stock'],'0')
        self.assertEqual(str(service.source.write_delta_once.await_args.args[2]['stock_increment']),'4')
        async with self.session() as db:
            point=await db.get(StockSyncCheckpoint,(self.shop_id,'SKU-A'))
            self.assertEqual((point.local_quantity,point.native_quantity),(0,-8))
            balance=await db.scalar(select(StockBalance).where(StockBalance.product_id==self.product_id))
            self.assertEqual(balance.qty_reserved,4,'The remaining four units are backordered, not reserved')

    async def test_acknowledgement_advances_only_frozen_intent_then_next_run_publishes_new_change(self):
        row=await self.prepare()
        async def received_during_put(*args):
            acknowledgement=await self.write_remote(*args)
            async with self.session() as db:
                balance=await db.scalar(select(StockBalance).where(StockBalance.product_id==self.product_id))
                balance.qty_on_hand+=2
            return acknowledgement
        service.source.write_delta_once.side_effect=received_during_put
        await self.invoke('process_run',row['id'])
        self.assertEqual(self.remote['stock'],'4')
        async with self.session() as db:
            point=await db.get(StockSyncCheckpoint,(self.shop_id,'SKU-A'))
            self.assertEqual(point.local_quantity,4,'ACK must not swallow the concurrently received units')
        service.source.write_delta_once.side_effect=self.write_remote
        second=await self.invoke('enqueue',SyncRunInput(shop_code='biketrek',confirmed=True,skus=['SKU-A']))
        await self.invoke('process_run',second['id'])
        self.assertEqual(self.remote['stock'],'6')
        self.assertEqual([str(call.args[2]['stock_increment']) for call in service.source.write_delta_once.await_args_list],['-5','2'])

    async def test_missing_checkpoint_never_infers_baseline_from_live_stock(self):
        async with self.session() as db:
            await db.delete(await db.get(StockSyncCheckpoint,(self.shop_id,'SKU-A')))
        row=await self.prepare()
        await self.invoke('process_run',row['id'])
        result=await self.invoke('get_run',row['id'])
        self.assertEqual(result['items'][0]['error'],'stock_sync_baseline_required')
        service.source.read_many.assert_not_awaited()
        service.source.read.assert_not_awaited()
        service.source.write_delta_once.assert_not_awaited()

    async def test_baseline_requires_active_hold_and_matching_confirmed_quantity(self):
        async with self.session() as db:
            await db.delete(await db.get(StockSyncCheckpoint,(self.shop_id,'SKU-A')))
        request=SyncBaselineInput(shop_code='biketrek',hold_id=self.hold_id,skus=['SKU-A'],
            confirmed=True,external_writers_paused=True,orders_reconciled=True)
        with self.assertRaises(service.SyncError) as raised:
            await self.invoke('baseline',request)
        self.assertEqual(raised.exception.code,'stock_sync_baseline_hold_required')
        service.source.read_many.assert_not_awaited()
        async with self.session() as db:
            hold=await db.get(StockPublicationHold,self.hold_id)
            hold.active,hold.created_at,hold.closed_at=True,self.clock,None
        with self.assertRaises(service.SyncError) as raised:
            await self.invoke('baseline',request)
        self.assertEqual(raised.exception.code,'stock_sync_baseline_mismatch')
        async with self.session() as db:
            self.assertIsNone(await db.get(StockSyncCheckpoint,(self.shop_id,'SKU-A')))
        self.remote.update(stock='4',availability='SKLADOM')
        result=await self.invoke('baseline',request)
        self.assertEqual(result['baseline_count'],1)
        async with self.session() as db:
            point=await db.get(StockSyncCheckpoint,(self.shop_id,'SKU-A'))
            self.assertEqual((point.local_quantity,point.native_quantity),(4,0))
            self.assertEqual(point.target,self.remote['identity'])
            self.assertTrue((await db.get(StockPublicationHold,self.hold_id)).active)
        service.source.write_delta_once.assert_not_awaited()

    async def test_baseline_rejects_peer_collection_before_hold_and_unprocessed_peer_order(self):
        async with self.session() as db:
            await db.delete(await db.get(StockSyncCheckpoint,(self.shop_id,'SKU-A')))
            hold=await db.get(StockPublicationHold,self.hold_id)
            hold.active,hold.created_at,hold.closed_at=True,self.clock,None
            peer=await db.scalar(select(Shop).where(Shop.code=='xtrek'))
            peer_id=peer.id
            fingerprint=order_collection.target_fingerprint(peer.code)
            db.add(OrderStockPolicy(shop_id=peer.id,warehouse_id=self.warehouse_id,
                starts_at=self.clock-timedelta(days=30),revision=1,status_actions={},status_hash='a'*64,statuses=[]))
            db.add(StockShopSettings(shop_id=peer.id,revision=1,mode='reserve',overrides={},updated_at=self.clock,
                automation_starts_at=self.clock-timedelta(days=1),authorized_policy_revision=1,target_fingerprint=fingerprint))
            db.add(OrderCollectionSettings(shop_id=peer.id,enabled=True,revision=1,target_fingerprint=fingerprint,
                cursor_at=self.clock-timedelta(minutes=1),reconcile_cursor_at=self.clock,
                last_completed_at=self.clock,next_poll_at=self.clock))
        self.remote.update(stock='4',availability='SKLADOM')
        request=SyncBaselineInput(shop_code='biketrek',hold_id=self.hold_id,skus=['SKU-A'],
            confirmed=True,external_writers_paused=True,orders_reconciled=True)
        with self.assertRaises(service.SyncError) as raised:
            await self.invoke('baseline',request)
        self.assertEqual(raised.exception.code,'stock_sync_baseline_orders_unsettled')
        async with self.session() as db:
            (await db.get(OrderCollectionSettings,peer_id)).cursor_at=self.clock
            db.add(OrderInboxEntry(shop_id=peer_id,source_uuid=str(uuid4()),order_number='PENDING-PEER',
                created_at=self.clock,updated_at=self.clock,deleted=False,origin='web',status_id=1,
                observation_hash='b'*64,observed_at=self.clock,last_seen_at=self.clock))
        with self.assertRaises(service.SyncError) as raised:
            await self.invoke('baseline',request)
        self.assertEqual(raised.exception.code,'stock_sync_baseline_orders_unsettled')
        service.source.read_many.assert_not_awaited()
        service.source.write_delta_once.assert_not_awaited()
        async with self.session() as db:
            self.assertIsNone(await db.get(StockSyncCheckpoint,(self.shop_id,'SKU-A')))

    async def test_baseline_cannot_start_without_order_automation_cutoff(self):
        async with self.session() as db:
            hold=await db.get(StockPublicationHold,self.hold_id)
            hold.active,hold.created_at,hold.closed_at=True,self.clock,None
            processing=await db.get(StockShopSettings,self.shop_id)
            processing.mode,processing.automation_starts_at='manual',None
        self.remote.update(stock='4',availability='SKLADOM')
        with self.assertRaises(service.SyncError) as raised:
            await self.invoke('baseline',SyncBaselineInput(shop_code='biketrek',hold_id=self.hold_id,
                skus=['SKU-A'],confirmed=True,external_writers_paused=True,orders_reconciled=True))
        self.assertEqual(raised.exception.code,'stock_sync_baseline_orders_unsettled')
        service.source.read_many.assert_not_awaited()
        service.source.write_delta_once.assert_not_awaited()
        async with self.session() as db:
            self.assertEqual((await db.get(StockSyncCheckpoint,(self.shop_id,'SKU-A'))).local_quantity,9)

    async def test_publisher_compensates_released_native_debt_without_manufacturing_return(self):
        # The real issued-order cancellation/exception workflow is covered by
        # test_stock_native_db. Here its boundary is a frozen acknowledged -2
        # native effect becoming 0 while physical availability remains two.
        async with self.session() as db:
            balance=await db.scalar(select(StockBalance).where(StockBalance.product_id==self.product_id))
            balance.qty_on_hand,balance.qty_reserved=3,0
        await self.checkpoint(local=2,native=-2)
        self.remote.update(stock='4',availability='SKLADOM')
        row=await self.prepare()
        async with self.session() as db:
            before={table:(await db.execute(text(f'SELECT * FROM {table} ORDER BY id'))).mappings().all()
                    for table in ('stock_balances','stock_movements')}
        await self.invoke('process_run',row['id'])
        self.assertEqual((await self.invoke('get_run',row['id']))['status'],'completed')
        self.assertEqual(self.remote['stock'],'2')
        self.assertEqual(service.source.write_delta_once.await_args.args[2]['stock_increment'],'-2')
        async with self.session() as db:
            point=await db.get(StockSyncCheckpoint,(self.shop_id,'SKU-A'))
            self.assertEqual((point.local_quantity,point.native_quantity),(2,0))
            after={table:(await db.execute(text(f'SELECT * FROM {table} ORDER BY id'))).mappings().all()
                   for table in before}
        self.assertEqual(after,before)

    async def test_changed_pre_cutover_order_blocks_only_when_newer_than_baseline(self):
        async with self.session() as db:
            entry=OrderInboxEntry(shop_id=self.shop_id,source_uuid=str(uuid4()),order_number='OLD-NATIVE',
                created_at=self.clock-timedelta(days=31),updated_at=self.clock-timedelta(days=2),
                deleted=False,origin='web',status_id=1,observation_hash='b'*64,
                observed_at=self.clock,last_seen_at=self.clock)
            db.add(entry);await db.flush();entry_id=entry.id
        await self.prepare()
        async with self.session() as db:
            (await db.get(OrderInboxEntry,entry_id)).updated_at=self.clock
        with self.assertRaises(service.SyncError) as raised:
            await self.invoke('enqueue',SyncRunInput(shop_code='biketrek',confirmed=True,skus=['SKU-A']))
        self.assertEqual(raised.exception.code,'stock_native_historical_change')
        service.source.read_many.assert_not_awaited()
        service.source.write_delta_once.assert_not_awaited()

    async def test_peer_historical_change_uses_target_baseline_even_without_peer_checkpoint(self):
        async with self.session() as db:
            peer=await db.scalar(select(Shop).where(Shop.code=='xtrek'))
            fingerprint=order_collection.target_fingerprint(peer.code)
            db.add(OrderStockPolicy(shop_id=peer.id,warehouse_id=self.warehouse_id,
                starts_at=self.clock-timedelta(days=30),revision=1,status_actions={},status_hash='a'*64,statuses=[]))
            db.add(StockShopSettings(shop_id=peer.id,revision=1,mode='reserve',overrides={},updated_at=self.clock,
                automation_starts_at=self.clock-timedelta(days=1),authorized_policy_revision=1,target_fingerprint=fingerprint))
            db.add(OrderCollectionSettings(shop_id=peer.id,enabled=True,revision=1,target_fingerprint=fingerprint,
                cursor_at=self.clock,reconcile_cursor_at=self.clock,last_completed_at=self.clock,next_poll_at=self.clock))
            entry=OrderInboxEntry(shop_id=peer.id,source_uuid=str(uuid4()),order_number='PEER-OLD-NATIVE',
                created_at=self.clock-timedelta(days=31),updated_at=self.clock-timedelta(days=2),
                deleted=False,origin='web',status_id=1,observation_hash='b'*64,
                observed_at=self.clock,last_seen_at=self.clock)
            db.add(entry);await db.flush();entry_id=entry.id
            self.assertIsNone(await db.get(StockSyncCheckpoint,(peer.id,'SKU-A')))
        # BIKETREK was reconciled yesterday; older xTrek changes are absorbed.
        await self.prepare()
        async with self.session() as db:
            (await db.get(OrderInboxEntry,entry_id)).updated_at=self.clock
        with self.assertRaises(service.SyncError) as raised:
            await self.invoke('enqueue',SyncRunInput(shop_code='biketrek',confirmed=True,skus=['SKU-A']))
        self.assertEqual(raised.exception.code,'stock_native_historical_change')
        service.source.read_many.assert_not_awaited()
        service.source.write_delta_once.assert_not_awaited()

    async def test_audit_reports_unexplained_drift_without_overwriting_remote_quantity(self):
        self.remote.update(stock='5',availability='SKLADOM')
        await self.checkpoint(local=4,audit_due=True)
        row=await self.prepare()
        await self.invoke('process_run',row['id'])
        result=await self.invoke('get_run',row['id'])
        self.assertEqual(result['items'][0]['error'],'stock_sync_audit_mismatch')
        self.assertEqual(self.remote['stock'],'5')
        service.source.write_delta_once.assert_not_awaited()
        async with self.session() as db:
            point=await db.get(StockSyncCheckpoint,(self.shop_id,'SKU-A'))
            self.assertEqual(point.local_quantity,4)
            self.assertTrue(point.last_observation['audit_mismatch'])

    async def test_metadata_acknowledgement_does_not_hide_observed_quantity_drift(self):
        self.remote.update(stock='5',availability='overíme')
        await self.checkpoint(local=4,metadata={'availability':'overíme','can_add_to_basket_yn':True})
        row=await self.prepare()
        await self.invoke('process_run',row['id'])
        result=await self.invoke('get_run',row['id'])
        self.assertEqual((result['status'],result['items'][0]['status'],result['items'][0]['error']),
            ('partial','skipped','stock_sync_audit_mismatch'))
        self.assertTrue(result['items'][0]['after']['acknowledged'])
        self.assertEqual((self.remote['stock'],self.remote['availability']),('5','SKLADOM'))
        service.source.write_delta_once.assert_awaited_once()
        self.assertEqual(service.source.write_delta_once.await_args.args[2]['stock_increment'],'0')
        async with self.session() as db:
            point=await db.get(StockSyncCheckpoint,(self.shop_id,'SKU-A'))
            self.assertEqual(point.local_quantity,4)
            self.assertEqual(point.metadata_values['availability'],'SKLADOM')
            self.assertTrue(point.last_observation['audit_mismatch'])
        second=await self.invoke('enqueue',SyncRunInput(shop_code='biketrek',confirmed=True,skus=['SKU-A']))
        await self.invoke('process_run',second['id'])
        self.assertEqual((await self.invoke('get_run',second['id']))['items'][0]['error'],'stock_sync_audit_mismatch')
        self.assertEqual(service.source.read_many.await_count,2,'Unresolved drift must remain subject to auditing')
        service.source.write_delta_once.assert_awaited_once()

    async def test_ambiguous_delta_needs_explicit_outcome_and_not_applied_keeps_checkpoint(self):
        row=await self.prepare()
        service.source.write_delta_once.side_effect=service.source.SourceError(
            'stock_publication_write_unconfirmed',uncertain=True)
        await self.invoke('process_run',row['id'])
        result=await self.invoke('get_run',row['id'])
        self.assertEqual(result['status'],'uncertain')
        with self.assertRaises(service.SyncError) as raised:
            await self.invoke('resolve',result['items'][0]['id'],
                SyncResolveInput(confirmed=True,external_requests_finished=True))
        self.assertEqual(raised.exception.code,'stock_sync_delta_resolution_required')
        service.source.read.assert_not_awaited()
        async with self.session() as db:
            self.assertEqual((await db.get(StockSyncCheckpoint,(self.shop_id,'SKU-A'))).local_quantity,9)
            self.assertEqual((await db.get(StockSyncItem,result['items'][0]['id'])).status,'uncertain')
        await self.invoke('resolve',result['items'][0]['id'],SyncResolveInput(confirmed=True,
            external_requests_finished=True,outcome='not_applied',evidence='Upstream request log confirms rejection before application'))
        async with self.session() as db:
            self.assertEqual((await db.get(StockSyncCheckpoint,(self.shop_id,'SKU-A'))).local_quantity,9)
        service.source.write_delta_once.side_effect=self.write_remote
        second=await self.invoke('enqueue',SyncRunInput(shop_code='biketrek',confirmed=True,skus=['SKU-A']))
        await self.invoke('process_run',second['id'])
        self.assertEqual(self.remote['stock'],'4')
        self.assertEqual(service.source.write_delta_once.await_count,2,'One ambiguous attempt, then one explicitly authorized new intent')

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
        service.source.write_delta_once.assert_not_awaited()

    async def test_ambiguous_write_fences_target_and_maintenance_until_resolved(self):
        row=await self.prepare()
        async def uncertain(*args):
            await self.write_remote(*args)
            raise service.source.SourceError('stock_publication_write_unconfirmed',uncertain=True)
        service.source.write_delta_once.side_effect=uncertain
        await self.invoke('process_run',row['id'])
        result=await self.invoke('get_run',row['id'])
        self.assertEqual(result['status'],'uncertain')
        self.assertEqual(self.remote['stock'],'4','A matching remote value still cannot prove the request outcome')
        async with self.session() as db:
            self.assertEqual((await db.get(StockSyncCheckpoint,(self.shop_id,'SKU-A'))).local_quantity,9)
        with self.assertRaises(service.SyncError) as raised:
            await self.invoke('resolve',result['items'][0]['id'],
                SyncResolveInput(confirmed=True,external_requests_finished=True))
        self.assertEqual(raised.exception.code,'stock_sync_delta_resolution_required')
        service.source.read.assert_not_awaited()
        await self.invoke('recover')
        async with self.session() as db:
            with self.assertRaises(stock_publication.PublicationError) as raised:
                await stock_publication.open_hold(db,StockPublicationOpenHold(shop_code='biketrek',confirmed=True,
                                                    external_writers_paused=True,orders_reconciled=True))
            self.assertEqual(raised.exception.code,'stock_sync_inflight')
        second=await self.invoke('enqueue',SyncRunInput(shop_code='biketrek',confirmed=True,skus=['SKU-A']))
        await self.invoke('process_run',second['id'])
        self.assertEqual(service.source.write_delta_once.await_count,1)
        resolved=await self.invoke('resolve',result['items'][0]['id'],SyncResolveInput(confirmed=True,external_requests_finished=True,outcome='applied',evidence='Confirmed synthetic upstream request was applied once'))
        self.assertEqual(resolved['status'],'completed');self.assertEqual(service.source.write_delta_once.await_count,1)
        async with self.session() as db:
            self.assertEqual((await db.get(StockSyncCheckpoint,(self.shop_id,'SKU-A'))).local_quantity,4)

    async def test_stock_changed_during_remote_read_is_reprojected_before_put(self):
        row=await self.prepare()
        first=True
        async def read(*args):
            nonlocal first
            observed=await self.read_many_remote(*args)
            if first:
                first=False
                async with self.session() as db:
                    balance=await db.scalar(select(StockBalance).where(StockBalance.product_id==self.product_id))
                    balance.qty_reserved=3
            return observed
        service.source.read_many.side_effect=read
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
        service.source.write_delta_once.assert_not_awaited()

    async def test_cancellation_after_dispatch_is_durable_uncertainty(self):
        row=await self.prepare()
        service.source.write_delta_once.side_effect=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.invoke('process_run',row['id'])
        await self.invoke('recover')
        await self.invoke('process_run',row['id'])
        result=await self.invoke('get_run',row['id'])
        self.assertEqual(result['items'][0]['status'],'uncertain')
        self.assertEqual(service.source.write_delta_once.await_count,1)

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
        service.source.write_delta_once.assert_not_awaited()

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
        service.source.write_delta_once.assert_not_awaited()


    async def test_authority_configure_rejects_unresolved_ai_availability_update(self):
        from inventory_hub.ai_content_models import AiBatch, AiJob
        async with self.session() as db:
            batch=AiBatch(id=uuid4().hex,request_hash='a'*64)
            db.add(batch);await db.flush()
            db.add(AiJob(id=uuid4().hex,batch_id=batch.id,kind='product',status='review',
                context={'shop':'biketrek','code':'xTrek','update_preview':{'fields':['availability'],'state':'uncertain'}},
                checks={},events=[],usage={},reserved_usd=0))
        with self.assertRaises(service.SyncError) as raised:
            await self.invoke('configure',self.config())
        self.assertEqual(raised.exception.code,'stock_sync_ai_availability_inflight')
        async with self.session() as db:
            self.assertIsNone(await db.get(StockSyncSettings,self.shop_id))
