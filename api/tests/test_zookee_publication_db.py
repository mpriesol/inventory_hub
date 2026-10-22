"""Scoped supplier availability uses the existing durable send/recovery protocol."""
import copy
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy import select
from inventory_hub import config_io
from inventory_hub.db_models import SupplierProduct, Product, ProductIdentifier, IdentifierType, SupplierFeed, SupplierFeedRun, FeedRunStatus
from inventory_hub.db_models_ext import ProductSupplySource, ShopProduct
from inventory_hub.product_editor_models import ProductEditorPublication
from inventory_hub.supplier_availability_models import SupplierAvailabilitySettings, SupplierAvailabilityObservation
from inventory_hub.services import supplier_availability_publication as service
from inventory_hub.services import product_publication as publication
from inventory_hub.services import product_publication_source as source
from inventory_hub.services import merchandising_write_guard as guard
import test_product_publication_db as fixture


@unittest.skipUnless(os.environ.get('CATALOG_TEST_DATABASE_URL'), 'Requires isolated localhost *_catalog_test database')
class SupplierPublicationDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixture.PublicationDatabaseTests.asyncSetUp(self)
        async with self.engine.begin() as connection:
            raw = await connection.get_raw_connection()
            await raw.driver_connection.execute((self.sql_root / '015_stock_sync.sql').read_text())
        self.folder = tempfile.TemporaryDirectory()
        self.stack = ExitStack(); self.stack.enter_context(patch.object(config_io, 'DATA_ROOT', Path(self.folder.name)))
        self.policy = {'enabled':True,'shop_code':'biketrek','brands':['SENSOR','TOPEAK']}
        self.config = {'adapter_settings': {'availability_publication':self.policy, 'availability':{'orderable':'do 5 dní','unavailable':'vypredané','unknown':'overíme'}}}
        p = config_io.supplier_path('editor-supplier'); p.parent.mkdir(parents=True); p.write_text(json.dumps(self.config))
        async with self.sessions() as db:
            source_row = await db.scalar(select(SupplierProduct).where(SupplierProduct.supplier_id == self.supplier_id))
            source_row.brand = 'TOPEAK'; self.sku = source_row.supplier_sku
            mapping = await db.scalar(select(ShopProduct).where(ShopProduct.product_id == self.products['BIKE-2'], ShopProduct.shop_id == self.shops['biketrek']))
            mapping.external_variant_id = '22'
            await db.commit()
        self.remote = {'identity':{'code':'BIKE-2','variant_code':'BIKE-2','parent_code':'xTrek','product_id':11,'variant_id':22},
                       'options':None,'leaf':{'code':'BIKE-2','stock':'0','availability':'do 5 dní'}}
        self.stack.enter_context(patch.object(publication, 'fingerprint', return_value='a'*64))
        self.stack.enter_context(patch.object(source, 'read_remote', AsyncMock(side_effect=lambda *a, **k:copy.deepcopy(self.remote))))
        self.observation = self.stack.enter_context(patch.object(service.availability, 'source_observation', AsyncMock(return_value={'fresh':True,'available':False})))
        self.writer = self.stack.enter_context(patch.object(source, 'write_once', AsyncMock(side_effect=self.write)))
    transaction = fixture.PublicationDatabaseTests.transaction
    detail = fixture.PublicationDatabaseTests.detail
    change = fixture.PublicationDatabaseTests.change
    save = fixture.PublicationDatabaseTests.save

    async def asyncTearDown(self):
        self.stack.close(); self.folder.cleanup()
        await fixture.PublicationDatabaseTests.asyncTearDown(self)

    async def write(self, shop, identity, fingerprint, payload):
        leaf = payload['products'][0]['variants'][0]
        self.assertEqual(set(leaf), {'code','availability'}, 'Supplier stock never overwrites own quantities or prices')
        self.remote['leaf']['availability'] = leaf['availability']

    async def link_sibling_source(self, db):
        source_row = SupplierProduct(supplier_id=self.supplier_id, supplier_sku=self.sku + '-SIBLING',
            name='Second physical variant', brand='TOPEAK')
        db.add(source_row)
        await db.flush()
        db.add(ProductSupplySource(product_id=self.products['BIKE-10'], supplier_product_id=source_row.id))
        return source_row

    async def prepare(self):
        async with self.sessions() as db:
            await service._prepare_and_send(db, self.products['BIKE-2'], self.shops['biketrek'], 'editor-supplier', self.sku, 999, self.policy, self.config)
        async with self.sessions() as db:
            return await db.scalar(select(ProductEditorPublication).order_by(ProductEditorPublication.created_at.desc()))

    async def test_exact_variant_availability_only_and_repeated_send(self):
        record = await self.prepare()
        self.assertEqual(record.state, 'completed', record.document); self.assertEqual(self.writer.await_count, 1)
        self.assertEqual(self.remote['leaf']['stock'], '0')
        async with self.sessions() as db:
            await publication.send(db, record.id)
        self.assertEqual(self.writer.await_count, 1)

    async def test_uncertain_write_keeps_original_fence(self):
        self.writer.side_effect = source.source.SourceError('product_publication_write_unconfirmed', uncertain=True)
        record = await self.prepare()
        self.assertEqual(record.state, 'uncertain', record.document)
        async with self.sessions() as db:
            await publication.send(db, record.id)
        self.assertEqual(self.writer.await_count, 1)

    async def test_expired_source_and_wrong_remote_id_never_send(self):
        self.observation.return_value = {'fresh':False,'available':None}
        record = await self.prepare(); self.assertEqual(record.state, 'rejected'); self.writer.assert_not_awaited()
        self.observation.return_value = {'fresh':True,'available':False}
        self.remote['identity']['variant_id'] = 123
        record = await self.prepare(); self.assertEqual(record.state, 'rejected'); self.writer.assert_not_awaited()

    async def test_ai_and_full_stock_authority_cannot_compete_with_policy(self):
        async with self.sessions() as db:
            with self.assertRaises(Exception) as error:
                await guard.require_target_available(db, 'biketrek', 'xTrek', availability=True)
            self.assertEqual(error.exception.code, 'supplier_availability_managed_by_supplier')
        async with self.sessions() as db:
            with self.assertRaises(Exception) as error:
                await guard.require_availability_authority_available(db, 'biketrek')
            self.assertEqual(error.exception.code, 'supplier_availability_publication_enabled')

    async def test_configured_matching_links_only_same_brand_exact_ean_and_shop(self):
        from inventory_hub.services.supplier_catalog_matching import suggestions
        from inventory_hub.services.supplier_links import reconcile_supplier_links
        self.config['adapter_settings']['existing_card_matching'] = self.policy
        self.config['adapter_settings']['mapping'] = {'postprocess':{'product_code_prefix':'ZOO-'}}
        config_io.supplier_path('editor-supplier').write_text(json.dumps(self.config))
        async with self.sessions() as db:
            product = await db.get(Product,self.products['BIKE-10']); product.brand='TOPEAK'
            db.add(ProductIdentifier(product_id=product.id,identifier_type=IdentifierType.ean,value='4006381333931'))
            db.add(SupplierProduct(supplier_id=self.supplier_id,supplier_sku='OLDER-1',brand='TOPEAK',name='Synthetic older item',ean='4006381333931'))
            await db.commit()
        async with self.sessions() as db:
            result = await reconcile_supplier_links(db,'editor-supplier',product_ids=[self.products['BIKE-10']],match_catalog=True)
            self.assertEqual(result['linked'],1,result)
            await db.commit()
        async with self.sessions() as db:
            result = await reconcile_supplier_links(db,'editor-supplier',product_ids=[self.products['BIKE-10']],match_catalog=True)
            self.assertEqual(result['linked'],0);self.assertEqual(result['existing'],1)

    async def test_worker_skips_unverified_links_without_poisoning_batch_and_resumes_after_repair(self):
        from inventory_hub.services import product_editor
        self.config['feeds'] = {'sources':{'stock':{'mode':'remote','remote':{'url':'https://supplier.example.test/stock.xml'}}}}
        config_io.supplier_path('editor-supplier').write_text(json.dumps(self.config))
        now = product_editor.now()
        async with self.sessions() as db:
            feed=SupplierFeed(supplier_id=self.supplier_id,code='stock',name='Synthetic stock',feed_type='stock',source_format='xml',mapping_config={})
            db.add(feed);await db.flush()
            run=SupplierFeedRun(feed_id=feed.id,run_number=1,status=FeedRunStatus.completed,started_at=now,finished_at=now)
            db.add(run);await db.flush()
            db.add(SupplierAvailabilitySettings(supplier_id=self.supplier_id,feed_key='stock',enabled=True,next_run_at=now+timedelta(hours=1),last_success_at=now))
            db.add(SupplierAvailabilityObservation(supplier_id=self.supplier_id,supplier_sku=self.sku,feed_id=feed.id,run_id=run.id,
                available=False,quantity=0,quantity_kind='exact',raw={},observed_at=now,expires_at=now+timedelta(hours=1)))
            # This sibling has supplier evidence but no verified shop ID yet.
            second_source = await self.link_sibling_source(db)
            db.add(SupplierAvailabilityObservation(supplier_id=self.supplier_id, supplier_sku=second_source.supplier_sku,
                feed_id=feed.id, run_id=run.id, available=False, quantity=0, quantity_kind='exact', raw={},
                observed_at=now, expires_at=now+timedelta(hours=1)))
            await db.commit()
        requested = []
        async def read_batch(shop, documents, fingerprint):
            codes = [document['identity']['code'] for document in documents]; requested.extend(codes)
            self.assertEqual(len(codes), 1, 'An unverified sibling must not enter the API batch')
            remote = copy.deepcopy(self.remote); code = codes[0]
            remote['identity'].update(code=code, variant_code=code, variant_id=22 if code == 'BIKE-2' else 23)
            remote['leaf']['code'] = code
            return {code:remote}
        with patch.object(service,'get_session_context',self.transaction), patch.object(service,'read_batch',AsyncMock(side_effect=read_batch)):
            self.assertTrue(await service.cycle())
            self.assertFalse(await service.cycle())
            self.assertEqual(set(requested), {'BIKE-2'})
            async with self.sessions() as db:
                mapping = await db.scalar(select(ShopProduct).where(ShopProduct.product_id == self.products['BIKE-10'],
                    ShopProduct.shop_id == self.shops['biketrek']))
                mapping.external_variant_id = '23'; await db.commit()
            self.assertTrue(await service.cycle(), 'Repair makes the same fresh observation eligible without a failed-attempt loop')
            self.assertFalse(await service.cycle())
            self.assertIn('BIKE-10', requested)
        self.assertEqual(self.writer.await_count,1)

    async def test_overview_counts_current_card_outcomes_and_keeps_unresolved_write_visible(self):
        from inventory_hub.services import supplier_availability
        now = service.editor.now()
        async with self.sessions() as db:
            for product, state, minutes, shop, workflow in [
                ('BIKE-2', 'rejected', 6, 'biketrek', service.WORKFLOW),
                ('BIKE-2', 'completed', 5, 'biketrek', service.WORKFLOW),
                ('BIKE-10', 'uncertain', 4, 'biketrek', service.WORKFLOW),
                ('BIKE-10', 'rejected', 3, 'biketrek', service.WORKFLOW),
                ('BIKE-2', 'rejected', 2, 'xtrek', service.WORKFLOW),
                ('BIKE-2', 'rejected', 1, 'biketrek', 'other_workflow')]:
                db.add(ProductEditorPublication(id=str(uuid4()), product_id=self.products[product], shop_id=self.shops[shop],
                    state=state, created_at=now-timedelta(minutes=minutes),
                    document={'workflow':workflow, 'supplier':'editor-supplier', 'shop_code':shop}))
            await db.commit()
        async with self.sessions() as db:
            value = next(row for row in (await supplier_availability.overview(db))['suppliers'] if row['supplier']=='editor-supplier')
            self.assertEqual(value['publication']['counts'], {'completed':1, 'uncertain':1})
            self.assertEqual(len(list(await db.scalars(select(ProductEditorPublication)))), 6, 'History remains intact')

    async def test_batch_send_never_repeats_uncertain(self):
        async with self.sessions() as db:
            first=await service._prepare_and_send(db,self.products['BIKE-2'],self.shops['biketrek'],
                'editor-supplier',self.sku,999,self.policy,self.config,send=False)
        # A replay through the batch service retains the same durable intent.
        async def read_batch(*args): return {'BIKE-2':copy.deepcopy(self.remote)}
        with patch.object(service,'read_batch',AsyncMock(side_effect=read_batch)):
            self.writer.side_effect=source.source.SourceError('product_publication_write_unconfirmed',uncertain=True)
            async with self.sessions() as db: await service.send_batch(db,[first])
            async with self.sessions() as db:
                record=await db.get(ProductEditorPublication,first)
                self.assertEqual(record.state,'uncertain',record.document)
                await service.send_batch(db,[first])
            self.assertEqual(self.writer.await_count,1)

    async def test_sibling_batch_has_one_write_and_individual_receipts(self):
        async with self.sessions() as db:
            await self.link_sibling_source(db)
            second=await db.get(Product,self.products['BIKE-10'])
            mapping=await db.scalar(select(ShopProduct).where(ShopProduct.product_id==second.id,ShopProduct.shop_id==self.shops['biketrek']))
            mapping.external_variant_id='23';await db.commit()
        remotes={'BIKE-2':copy.deepcopy(self.remote),'BIKE-10':copy.deepcopy(self.remote)}
        remotes['BIKE-10']['identity'].update(code='BIKE-10',variant_code='BIKE-10',variant_id=23)
        remotes['BIKE-10']['leaf']['code']='BIKE-10'
        ids=[]
        for code in ('BIKE-2','BIKE-10'):
            async with self.sessions() as db:
                ids.append(await service._prepare_and_send(db,self.products[code],self.shops['biketrek'],
                    'editor-supplier',self.sku if code == 'BIKE-2' else self.sku + '-SIBLING',999,self.policy,self.config,remote=remotes[code],send=False))
        async def read_batch(*args): return copy.deepcopy(remotes)
        async def write_batch(shop,items,fingerprint):
            self.assertEqual(len(items),2)
            async with self.sessions() as check:
                self.assertEqual({(await check.get(ProductEditorPublication,id)).state for id in ids},{'sending'})
            for item in items: remotes[item['identity']['code']]['leaf']['availability']=item['availability']
        with patch.object(service,'read_batch',AsyncMock(side_effect=read_batch)) as reader, patch.object(source,'write_availability_batch_once',AsyncMock(side_effect=write_batch)) as writer:
            async with self.sessions() as db: await service.send_batch(db,ids)
            async with self.sessions() as db:
                self.assertEqual([(await db.get(ProductEditorPublication,id)).state for id in ids],['completed','completed'],
                    [(await db.get(ProductEditorPublication,id)).document.get('error') for id in ids])
            writer.assert_awaited_once();self.assertEqual(reader.await_count,2)
            async with self.sessions() as db:
                self.assertEqual({(await db.get(ProductEditorPublication,id)).state for id in ids},{'completed'})
                await service.send_batch(db,ids)
            writer.assert_awaited_once();self.writer.assert_not_awaited()
