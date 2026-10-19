"""Native-ID verified administrative renames preserve the warehouse ledger."""
import copy
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from sqlalchemy import select, func
from inventory_hub import config_io
from inventory_hub.db_models import Product, IdentifierType, ProductIdentifier
from inventory_hub.db_models_ext import ShopProduct, StockMovement
from inventory_hub.product_editor_models import ProductEditorAudit, ProductEditorPublication
from inventory_hub.services import zookee_code_reconciliation as service
import test_product_publication_db as fixture


@unittest.skipUnless(os.environ.get('CATALOG_TEST_DATABASE_URL'), 'Requires isolated localhost *_catalog_test database')
class CodeReconciliationDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixture.PublicationDatabaseTests.asyncSetUp(self)
        async with self.engine.begin() as connection:
            raw=await connection.get_raw_connection()
            for name in ('010_stock_publication.sql','015_stock_sync.sql'):
                await raw.driver_connection.execute((self.sql_root/name).read_text())
        self.folder=tempfile.TemporaryDirectory();self.stack=ExitStack()
        self.stack.enter_context(patch.object(config_io,'DATA_ROOT',Path(self.folder.name)))
        path=config_io.supplier_path('zookee');path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'adapter_settings':{'mapping':{'postprocess':{'product_code_prefix':'ZOO-'}}}}))
        self.pid=self.products['BIKE-2']
        async with self.sessions() as db:
            (await db.get(Product,self.pid)).sku='TOPEAK-1'
            for mapping in await db.scalars(select(ShopProduct).where(ShopProduct.product_id == self.pid)):
                mapping.variant_code='TOPEAK-1';mapping.external_id='22' if mapping.shop_id == self.shops['biketrek'] else '23'
            self.movement_count=await db.scalar(select(func.count()).select_from(StockMovement));await db.commit()
        self.stack.enter_context(patch.object(service.publication,'fingerprint',return_value='a'*64))
        self.reader=self.stack.enter_context(patch.object(service.transport,'read_remote',AsyncMock(side_effect=self.read)))
    transaction=fixture.PublicationDatabaseTests.transaction
    detail=fixture.PublicationDatabaseTests.detail
    change=fixture.PublicationDatabaseTests.change
    save=fixture.PublicationDatabaseTests.save

    async def asyncTearDown(self):
        self.stack.close();self.folder.cleanup();await fixture.PublicationDatabaseTests.asyncTearDown(self)

    async def read(self,shop,target,fingerprint):
        return {'identity':{**target,'variant_id':22 if shop=='biketrek' else 23,'product_id':11},
                'leaf':{'code':'ZOO-1','ean':'5901234123457'},'options':None}

    async def test_all_shop_ids_verified_local_identity_audited_and_retry_is_read_only(self):
        request_id=uuid4()
        async with self.sessions() as db:
            result=await service.reconcile_one(db,'zookee',self.pid,request_id)
        self.assertEqual(result['new_code'],'ZOO-1');self.assertEqual(self.reader.await_count,2)
        async with self.sessions() as db:
            self.assertEqual((await db.get(Product,self.pid)).sku,'ZOO-1')
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)),self.movement_count)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductEditorAudit)),1)
            self.assertIsNotNone(await db.scalar(select(ProductIdentifier).where(ProductIdentifier.product_id==self.pid,
                ProductIdentifier.identifier_type==IdentifierType.internal_sku,ProductIdentifier.value=='TOPEAK-1')))
            self.assertEqual(await service.reconcile_one(db,'zookee',self.pid,request_id),result)
        self.assertEqual(self.reader.await_count,2)

    async def test_native_id_mismatch_or_partial_shop_rename_cannot_change_local_identity(self):
        async def wrong(*args):
            result=await self.read(*args);result['identity']['variant_id']=999;return result
        self.reader.side_effect=wrong
        async with self.sessions() as db:
            with self.assertRaises(service.editor.EditorError):await service.reconcile_one(db,'zookee',self.pid,uuid4())
        async with self.sessions() as db:self.assertEqual((await db.get(Product,self.pid)).sku,'TOPEAK-1')
        async def partial(shop,*args):
            if shop=='xtrek':raise service.transport.source.SourceError('stock_publication_target_missing')
            return await self.read(shop,*args)
        self.reader.side_effect=partial
        async with self.sessions() as db:
            with self.assertRaises(service.transport.source.SourceError):await service.reconcile_one(db,'zookee',self.pid,uuid4())
        async with self.sessions() as db:self.assertEqual((await db.get(Product,self.pid)).sku,'TOPEAK-1')

    async def test_target_collision_cannot_merge_two_canonical_products(self):
        async with self.sessions() as db:
            db.add(Product(sku='ZOO-1',name='Different product'));await db.commit()
        async with self.sessions() as db:
            with self.assertRaises(service.editor.EditorError) as error:await service.reconcile_one(db,'zookee',self.pid,uuid4())
            self.assertEqual(error.exception.code,'product_editor_identity_conflict')
        async with self.sessions() as db:self.assertEqual((await db.get(Product,self.pid)).sku,'TOPEAK-1')

    async def test_unresolved_publication_blocks_local_rename(self):
        async with self.sessions() as db:
            db.add(ProductEditorPublication(id=str(uuid4()),product_id=self.pid,shop_id=self.shops['biketrek'],state='uncertain',
                document={'identity':{'parent_code':'xTrek'}}));await db.commit()
        async with self.sessions() as db:
            with self.assertRaises(Exception) as error:await service.reconcile_one(db,'zookee',self.pid,uuid4())
            self.assertEqual(error.exception.code,'merchandising_target_inflight')
        async with self.sessions() as db:self.assertEqual((await db.get(Product,self.pid)).sku,'TOPEAK-1')
