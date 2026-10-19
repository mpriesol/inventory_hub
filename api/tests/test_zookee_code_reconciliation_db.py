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
from types import SimpleNamespace
from sqlalchemy import select, func
from inventory_hub import config_io
from inventory_hub.db_models import Product, IdentifierType, ProductIdentifier
from inventory_hub.db_models_ext import ShopProduct, ShopProductContent, StockMovement
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

    async def missing_id_fixture(self, *, variant=True):
        async with self.sessions() as db:
            for mapping in await db.scalars(select(ShopProduct).where(ShopProduct.product_id == self.pid)):
                cache = await db.scalar(select(ShopProductContent).where(
                    ShopProductContent.shop_id == mapping.shop_id,
                    ShopProductContent.external_code == mapping.parent_code))
                leaf = {'code': 'TOPEAK-1', 'product_id': 11, 'ean': '5901234123457'}
                if variant:
                    leaf['variant_id'] = int(mapping.external_id)
                    cache.data = {'code': mapping.parent_code, 'product_id': 11, 'variants': [leaf]}
                else:
                    mapping.is_variant = False; mapping.parent_code = None; mapping.variant_code = None
                    mapping.external_code = 'TOPEAK-1'; cache.external_code = 'TOPEAK-1'; cache.data = leaf
                mapping.external_id = None
            await db.commit()

    async def test_missing_variant_ids_are_recovered_from_original_cache_and_audited_with_replay(self):
        await self.missing_id_fixture()
        request = uuid4()
        async with self.sessions() as db:
            result = await service.reconcile_one(db, 'zookee', self.pid, request)
        async with self.sessions() as db:
            mappings = list(await db.scalars(select(ShopProduct).where(ShopProduct.product_id == self.pid)))
            self.assertEqual({m.external_id for m in mappings}, {'22', '23'})
            self.assertEqual({m.variant_code for m in mappings}, {'ZOO-1'})
            for cache in await db.scalars(select(ShopProductContent)):
                self.assertEqual(cache.data['variants'][0]['code'], 'ZOO-1')
            audit = await db.scalar(select(ProductEditorAudit))
            self.assertTrue(all(item['cached_identity'] for item in audit.after_data['native_identities']))
            self.assertEqual(await service.reconcile_one(db, 'zookee', self.pid, request), result)
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)), self.movement_count)
        self.assertEqual(self.reader.await_count, 2)

    async def test_standalone_cache_id_is_verified_before_mapping_and_code_are_restored(self):
        await self.missing_id_fixture(variant=False)
        async def standalone(*args):
            result = await self.read(*args); result['identity']['variant_id'] = None; return result
        self.reader.side_effect = standalone
        async with self.sessions() as db:
            await service.reconcile_one(db, 'zookee', self.pid, uuid4())
        async with self.sessions() as db:
            for mapping in await db.scalars(select(ShopProduct).where(ShopProduct.product_id == self.pid)):
                self.assertEqual((mapping.external_id, mapping.external_code), ('11', 'ZOO-1'))
            for cache in await db.scalars(select(ShopProductContent)):
                self.assertEqual((cache.external_code, cache.data['code']), ('ZOO-1', 'ZOO-1'))

    async def test_missing_cache_ids_do_not_use_remote_id_or_consume_batch_requests(self):
        async with self.sessions() as db:
            for mapping in await db.scalars(select(ShopProduct).where(ShopProduct.product_id == self.pid)):
                mapping.external_id = None
            await db.commit()
        from inventory_hub.services import product_link_source
        with patch.object(product_link_source, 'discover', AsyncMock()) as discover:
            async with self.sessions() as db:
                result = await service.reconcile_batch(db, 'zookee', [SimpleNamespace(product_id=self.pid, request_id=uuid4())])
            self.assertEqual(result['results'][0]['error'], 'code_rename_cache_identity_missing')
            discover.assert_not_awaited(); self.reader.assert_not_awaited()

    async def test_cached_id_must_match_remote_parent_and_variant_ids(self):
        await self.missing_id_fixture()
        async def wrong(*args):
            result = await self.read(*args); result['identity']['product_id'] = 999; return result
        self.reader.side_effect = wrong
        async with self.sessions() as db:
            with self.assertRaisesRegex(service.editor.EditorError, 'code_rename_native_id_changed'):
                await service.reconcile_one(db, 'zookee', self.pid, uuid4())
        async with self.sessions() as db:
            self.assertEqual((await db.get(Product, self.pid)).sku, 'TOPEAK-1')
            self.assertTrue(all(m.external_id is None for m in await db.scalars(
                select(ShopProduct).where(ShopProduct.product_id == self.pid))))

    async def test_ambiguous_cached_leaf_and_barcode_conflicts_are_rejected_without_remote_reads(self):
        await self.missing_id_fixture()
        async with self.sessions() as db:
            cache = await db.scalar(select(ShopProductContent).where(ShopProductContent.shop_id == self.shops['biketrek']))
            original = copy.deepcopy(cache.data)
            cache.data = {**original, 'variants': original['variants'] * 2}; await db.commit()
        async with self.sessions() as db:
            with self.assertRaisesRegex(service.editor.EditorError, 'code_rename_cache_identity_missing'):
                await service.reconcile_one(db, 'zookee', self.pid, uuid4())
        async with self.sessions() as db:
            cache = await db.scalar(select(ShopProductContent).where(ShopProductContent.shop_id == self.shops['biketrek']))
            cache.data = {**original, 'variants': [{**original['variants'][0], 'ean': '4006381333931'}]}; await db.commit()
        async with self.sessions() as db:
            with self.assertRaisesRegex(service.editor.EditorError, 'code_rename_barcode_conflict'):
                await service.reconcile_one(db, 'zookee', self.pid, uuid4())
        self.reader.assert_not_awaited()

    async def test_cache_identity_change_during_remote_reads_rejects_restoration(self):
        await self.missing_id_fixture()
        async def changed(shop, *args):
            if shop == 'biketrek':
                async with self.sessions() as db:
                    cache = await db.scalar(select(ShopProductContent).where(ShopProductContent.shop_id == self.shops[shop]))
                    cache.data = {**cache.data, 'variants': [{**cache.data['variants'][0], 'variant_id': 999}]}
                    await db.commit()
            return await self.read(shop, *args)
        self.reader.side_effect = changed
        async with self.sessions() as db:
            with self.assertRaisesRegex(service.editor.EditorError, 'product_editor_changed'):
                await service.reconcile_one(db, 'zookee', self.pid, uuid4())
        async with self.sessions() as db:
            self.assertEqual((await db.get(Product, self.pid)).sku, 'TOPEAK-1')

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

    async def test_batch_uses_one_bounded_read_per_shop_and_replays_without_reads(self):
        from inventory_hub.services import product_link_source
        rows=[SimpleNamespace(product_id=self.pid,request_id=uuid4())]
        async def discover(shop,codes,fingerprint):
            self.assertEqual(codes,['ZOO-1'])
            return {'leaves':[{'code':'ZOO-1','parent_code':'xTrek' if shop=='biketrek' else 'REAL-FRAMES','parent_id':'11',
                'external_id':'22' if shop=='biketrek' else '23','is_variant':True,'eans':['5901234123457']}]}
        with patch.object(product_link_source,'discover',AsyncMock(side_effect=discover)) as reader:
            async with self.sessions() as db: result=await service.reconcile_batch(db,'zookee',rows)
            self.assertEqual(result['results'][0]['status'],'completed',result)
            self.assertEqual(reader.await_count,2)
            self.reader.assert_not_awaited()
            async with self.sessions() as db:
                again=await service.reconcile_batch(db,'zookee',rows)
                self.assertEqual(again,result)
                self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)),self.movement_count)
            self.assertEqual(reader.await_count,2)
