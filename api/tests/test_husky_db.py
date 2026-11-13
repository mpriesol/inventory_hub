"""Husky composite snapshots in the existing isolated catalog test database."""
from contextlib import asynccontextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import unittest

import asyncpg
import psycopg2
from sqlalchemy import func, select, text

import test_catalog_db as catalog_tests
from husky_fixtures import item, stock, page
from inventory_hub import config_io
from inventory_hub.supplier_availability_delivery_once_migrate import apply_once
from inventory_hub.adapters import husky_b2b
from inventory_hub.db_models import Product, Supplier, SupplierProduct, SupplierFeedRun, FeedRunStatus
from inventory_hub.services import catalog, husky_sync, supplier_availability as service
from inventory_hub.services.supplier_availability_source import AvailabilityError
from inventory_hub.supplier_availability_models import SupplierAvailabilityObservation, SupplierAvailabilitySettings
from inventory_hub.supplier_availability_types import SupplierAvailabilityInput, SupplierAvailabilityRunInput

TEST_URL = os.environ.get('CATALOG_TEST_DATABASE_URL', '')


@unittest.skipUnless(TEST_URL, 'Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB')
class HuskyDatabaseTests(unittest.IsolatedAsyncioTestCase):
    write_feed = catalog_tests.CatalogDatabaseTests.write_feed

    async def asyncSetUp(self):
        await catalog_tests.CatalogDatabaseTests.asyncSetUp(self)
        self.path = config_io.supplier_path('husky')
        self.feed = self.path.parent / 'feeds' / 'xml' / 'products.xml'
        self.stock = self.feed.parent / 'stock.xml'
        self.feed.parent.mkdir(parents=True)
        self.feed.write_text('<SHOP>' + item() + '</SHOP>')
        self.stock.write_text('<SHOP>' + stock() + '</SHOP>')
        self.cfg = {'name':'Husky fixture','feeds':{'sources':{
            'products':{'mode':'local','local_path':str(self.feed)},
            'stock':{'mode':'local','local_path':str(self.stock)}}},
            'adapter_settings':{'currency':'EUR','vat_rate':23,'availability':{'orderable':'do 2-4 dní'},'husky':{}}}
        self.path.write_text(json.dumps(self.cfg))
        async with self.tx() as db:
            await service.configure(db,'husky',SupplierAvailabilityInput(expected_revision=0,enabled=True,feed_key='products'))
            self.supplier_id=await db.scalar(select(Supplier.id).where(Supplier.code=='husky'))
            db.add(Product(sku='EXISTING-42',name='Own existing card',brand='Husky'))

    async def asyncTearDown(self):
        await catalog_tests.CatalogDatabaseTests.asyncTearDown(self)
        connection=await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            await connection.close()

    @asynccontextmanager
    async def tx(self):
        async with self.sessions() as db:
            async with db.begin():
                yield db

    async def plan(self):
        async with self.tx() as db:
            settings=await db.get(SupplierAvailabilitySettings,self.supplier_id)
            await service.request_run(db,'husky',SupplierAvailabilityRunInput(expected_revision=settings.revision))
        async with self.tx() as db:
            return await service.start_run(db,self.supplier_id)

    async def accept(self,plan,snapshot=None):
        records,size=husky_sync.fetch(plan) if snapshot is None else (snapshot,0)
        async with self.tx() as db:
            await service.accept_run(db,plan,records,size)
        return records

    async def test_atomic_catalog_observations_audit_and_idempotence_without_own_stock(self):
        plan=await self.plan()
        await self.accept(plan)
        async with self.tx() as db:
            source=await db.scalar(select(SupplierProduct))
            identity=source.id
            self.assertEqual(source.supplier_sku,'PUBLIC-1')
            detail=await catalog.catalog_detail(db,'husky',identity)
            self.assertIn('<CODE>PUBLIC-1</CODE>',detail['source_xml'])
            self.assertEqual(detail['product'].availability,'overíme')
            obs=await db.scalar(select(SupplierAvailabilityObservation))
            self.assertTrue(obs.available)
            self.assertIsNone(obs.quantity)
            settings=await db.get(SupplierAvailabilitySettings,self.supplier_id)
            self.assertEqual((settings.interval_seconds,settings.last_item_count),(86400,1))
            self.assertIsNotNone(settings.last_success_at)
            self.assertEqual(await db.scalar(select(func.count()).select_from(Product)),1)
            self.assertEqual(await db.scalar(select(Product.sku)),'EXISTING-42')
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM stock_movements')),0)
            run=await db.get(SupplierFeedRun,plan['run_id'])
            self.assertEqual((run.items_fetched,run.items_new,run.items_updated,run.items_unchanged),(1,1,0,0))
        with self.assertRaises(AvailabilityError):
            await self.accept(plan)
        repeat=await self.plan()
        await self.accept(repeat)
        async with self.tx() as db:
            self.assertEqual(await db.scalar(select(SupplierProduct.id)),identity)
            self.assertEqual(await db.scalar(select(func.count()).select_from(SupplierAvailabilityObservation)),1)
            run=await db.get(SupplierFeedRun,repeat['run_id'])
            self.assertEqual((run.items_fetched,run.items_new,run.items_updated,run.items_unchanged),(1,0,0,1))

    async def test_changed_config_and_schedule_reject_downloaded_snapshot(self):
        plan=await self.plan()
        snapshot,_=husky_sync.fetch(plan)
        self.cfg['adapter_settings']['vat_rate']=10
        self.path.write_text(json.dumps(self.cfg))
        with self.assertRaises(AvailabilityError) as error:
            await self.accept(plan,snapshot)
        self.assertEqual(error.exception.code,'supplier_availability_changed')
        async with self.tx() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(SupplierProduct)),0)
            await service.fail_run(db,self.supplier_id,plan['run_id'],error.exception.code)
            value=await service.configure(db,'husky',SupplierAvailabilityInput(expected_revision=1,enabled=True,feed_key='products',interval_seconds=3600,freshness_seconds=7200))
            self.assertEqual(value['interval_seconds'],3600)
        plan=await self.plan()
        snapshot,_=husky_sync.fetch(plan)
        async with self.tx() as db:
            await service.configure(db,'husky',SupplierAvailabilityInput(expected_revision=2,enabled=False,feed_key='products'))
        with self.assertRaises(AvailabilityError):
            await self.accept(plan,snapshot)

    async def test_deployment_installs_daily_policy_once_then_preserves_hourly_choice(self):
        sql_root=Path(__file__).resolve().parents[2]/'infra/db-init'
        async with self.engine.begin() as connection:
            raw=await connection.get_raw_connection()
            await raw.driver_connection.execute((sql_root/'016_product_publication.sql').read_text())
        sql=(sql_root/'034_supplier_availability_delivery.sql').read_text()
        def install(source):
            with psycopg2.connect(TEST_URL,options=f'-c search_path={self.schema}') as connection:
                with connection.cursor() as cursor:
                    return apply_once(cursor,source)
        async with self.tx() as db:
            settings=await db.get(SupplierAvailabilitySettings,self.supplier_id)
            settings.interval_seconds,settings.freshness_seconds=3600,7200
        # A failed installation must not leave a false completion checkpoint.
        with self.assertRaises(psycopg2.errors.DivisionByZero):
            install(sql+'\nSELECT 1/0;')
        self.assertTrue(install(sql))
        async with self.tx() as db:
            settings=await db.get(SupplierAvailabilitySettings,self.supplier_id)
            self.assertEqual((settings.interval_seconds,settings.freshness_seconds),(86400,172800))
            configured=await service.configure(db,'husky',SupplierAvailabilityInput(expected_revision=settings.revision,
                enabled=True,feed_key='products',interval_seconds=3600,freshness_seconds=7200))
        self.assertFalse(install(sql))
        self.assertFalse(install(sql))
        async with self.tx() as db:
            settings=await db.get(SupplierAvailabilitySettings,self.supplier_id)
            self.assertEqual((settings.interval_seconds,settings.freshness_seconds),(3600,7200))
            self.assertEqual((settings.revision,settings.next_run_at),(configured['revision'],configured['next_run_at']))

    async def test_incomplete_catalog_rolls_back_all_and_keeps_success(self):
        self.feed.write_text('<SHOP>'+item()+item('PUBLIC-2','MPN-2','4006381333931')+'</SHOP>')
        self.stock.write_text('<SHOP>'+stock()+stock('PUBLIC-2')+'</SHOP>')
        await self.accept(await self.plan())
        self.feed.write_text('<SHOP>'+item()+'</SHOP>')
        plan=await self.plan()
        with self.assertRaises(AvailabilityError) as error:
            await self.accept(plan)
        self.assertEqual(error.exception.code,'supplier_availability_incomplete_feed')
        async with self.tx() as db:
            await service.fail_run(db,self.supplier_id,plan['run_id'],error.exception.code)
            self.assertEqual(await db.scalar(select(func.count()).select_from(SupplierProduct).where(SupplierProduct.is_active)),2)
            self.assertEqual(await db.scalar(select(func.count()).select_from(SupplierAvailabilityObservation)),2)
            status=await service.latest_run(db,self.supplier_id,'products')
            self.assertEqual(status['status'],'failed')

    async def test_b2b_price_and_unknown_replace_supplier_quote_without_physical_receipt(self):
        self.cfg['adapter_settings']['husky']['purchase_price_basis']='net'
        self.path.write_text(json.dumps(self.cfg))
        plan=await self.plan()
        public,_=husky_sync.fetch(plan)
        quotes={q.code:q for q in husky_b2b.parse_page(husky_b2b.document(page().encode()),1)[0]}
        snapshot=husky_sync.combine(public.catalog_records,{'PUBLIC-1':True},quotes,self.cfg)
        await self.accept(plan,snapshot)
        async with self.tx() as db:
            source=await db.scalar(select(SupplierProduct))
            self.assertEqual(str(source.purchase_price),'55.0000')
            self.assertIsNone(source.stock_qty)
            value=await service.source_observation(db,'husky','PUBLIC-1')
            self.assertEqual((value['quantity'],value['quantity_kind'],value['label']),('11.000','minimum','do 2-4 dní'))
        plan=await self.plan()
        public,_=husky_sync.fetch(plan)
        quotes={q.code:q for q in husky_b2b.parse_page(husky_b2b.document(page(stock_value='-',price='-').encode()),1)[0]}
        snapshot=husky_sync.combine(public.catalog_records,{'PUBLIC-1':True},quotes,self.cfg)
        await self.accept(plan,snapshot)
        async with self.tx() as db:
            source=await db.scalar(select(SupplierProduct))
            self.assertIsNone(source.purchase_price)
            value=await service.source_observation(db,'husky','PUBLIC-1')
            self.assertIsNone(value['available'])
            self.assertEqual(value['label'],'overíme')
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM stock_movements')),0)
