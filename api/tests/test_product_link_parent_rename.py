"""Metadata-only parent correction against disposable PostgreSQL and fake sources."""
import asyncio
from copy import deepcopy
import os
import time
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select, text

import test_product_links_db as fixture
import test_product_link_source as source_fixture
from inventory_hub.db_models import IdentifierType, ProductIdentifier, Supplier, SupplierProduct
from inventory_hub.db_models_ext import ProductSupplySource, ShopProduct, ShopProductContent
from inventory_hub.product_link_types import ProductLinksPreviewRequest, ProductLinksRefreshRequest
from inventory_hub.services import product_links as links, product_link_parent_rename as rename


def match(pid=1, mid=11, variant=101, code='SHARED-A'):
    return {'product_id':pid, 'code':code, 'parent_code':'CURRENT-PARENT', 'is_variant':True,
            'reviewed_parent_rename':{'mapping_id':mid, 'native_product_id':800, 'native_variant_id':variant,
                'expected_parent_code':'PREVIOUS-PARENT', 'same_physical_item_confirmed':True,
                'identity_evidence':'Same verified native product and variant IDs; only the parent code was renamed.'}}


class ParentRenameInputTests(unittest.TestCase):
    def body(self):
        return {'request_id':uuid4(), 'shop_code':'xtrek', 'product_ids':[1], 'manual_matches':[match()]}

    def test_exact_positive_pins_and_confirmation_required(self):
        self.assertIsNotNone(ProductLinksPreviewRequest(**self.body()).manual_matches[0].reviewed_parent_rename)
        for key, value in [('mapping_id',0),('native_product_id',None),('native_variant_id','101'),
                           ('native_variant_id',True),('same_physical_item_confirmed',False),
                           ('expected_parent_code',' CURRENT-PARENT')]:
            body=self.body()
            body['manual_matches'][0]['reviewed_parent_rename'][key]=value
            with self.subTest(key=key,value=value), self.assertRaises(ValidationError):
                ProductLinksPreviewRequest(**body)

    def test_mixed_batches_and_unchanged_parent_are_rejected(self):
        cases=[]
        body=self.body(); body['product_ids'].append(2); cases.append(body)
        body=self.body(); body['manual_matches'][0]['reviewed_parent_rename']['expected_parent_code']='CURRENT-PARENT'; cases.append(body)
        body=self.body(); body['manual_matches'][0]['replace_deleted_variant']=True; cases.append(body)
        body=self.body(); body['manual_matches'][0]['is_variant']=False; cases.append(body)
        for body in cases:
            with self.subTest(body=body), self.assertRaises(ValidationError):
                ProductLinksPreviewRequest(**body)


class ParentRenameSourceTests(unittest.IsolatedAsyncioTestCase):
    setup_client = source_fixture.ProductLinkSourceTests.setup_client

    async def test_source_reads_exact_parent_family_and_inactive_siblings(self):
        sf=source_fixture
        parent=sf.parent_metadata()
        first=sf.detailed()
        sibling={**sf.detailed('LEAF-L',72),'active_yn':False}
        self.setup_client(sf.page('products',[parent]),sf.page('variants',[first]),
                          sf.page('variants',[first,sibling]))
        result=await links.source.inspect_parent_rename('xtrek',['OLD-PARENT','PARENT','LEAF-M'],['7'])
        self.assertEqual(result['requests'],3)
        self.assertEqual([v['external_id'] for v in result['family_leaves']],['72','71'])
        self.assertGreater(result['observation_started_monotonic'],0)
        self.assertEqual(self.session.calls[-1][1]['params'],
                         {'product_ids':'7','page':1,'current_page_items':100})
        self.assertTrue(all(call[0].endswith(('/products','/products/variants')) for call in self.session.calls))

    async def test_foreign_parent_missing_family_and_duplicate_native_id_fail_closed(self):
        sf=source_fixture
        for rows in ([],[{**sf.detailed(),'product_id':8}],[sf.detailed(),sf.detailed()]):
            with self.subTest(rows=rows):
                self.setup_client(sf.page('products',[sf.parent_metadata()]),sf.page('variants',[sf.detailed()]),
                                  sf.page('variants',rows))
                with self.assertRaises(links.source.SourceError):
                    await links.source.inspect_parent_rename('xtrek',['OLD-PARENT','PARENT','LEAF-M'],['7'])


@unittest.skipUnless(os.environ.get('CATALOG_TEST_DATABASE_URL'), 'Requires isolated localhost PostgreSQL')
class ParentRenameDatabaseTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown=fixture.ProductLinksDatabaseTests.asyncTearDown
    transaction=fixture.ProductLinksDatabaseTests.transaction
    discover_remote=fixture.ProductLinksDatabaseTests.discover_remote
    verify_remote=fixture.ProductLinksDatabaseTests.verify_remote
    request=fixture.ProductLinksDatabaseTests.request
    preview=fixture.ProductLinksDatabaseTests.preview
    apply=fixture.ProductLinksDatabaseTests.apply
    mappings=fixture.ProductLinksDatabaseTests.mappings

    async def asyncSetUp(self):
        await fixture.ProductLinksDatabaseTests.asyncSetUp(self)
        self.remote=[fixture.leaf('SHARED-A',101,eans=[fixture.EAN_A],parent_code='CURRENT-PARENT'),
                     fixture.leaf('SHARED-B',102,eans=[fixture.EAN_B],parent_code='CURRENT-PARENT')]
        self.family=[*deepcopy(self.remote),fixture.leaf('NO-REMOTE',103,eans=[],parent_code='CURRENT-PARENT')]
        self.observe=AsyncMock(side_effect=self.observe_remote)
        patcher=patch.object(links.source,'inspect_parent_rename',self.observe)
        patcher.start(); self.patches.append(patcher)
        async with self.transaction() as db:
            # The second card deliberately has no canonical GTIN. A verified
            # unchanged native mapping supplies identity without creating one.
            await db.execute(text('DELETE FROM product_identifiers WHERE product_id=:id'),{'id':self.ids[1]})
            maps=[ShopProduct(shop_id=self.shop_id,product_id=pid,external_product_id='800',
                    external_variant_id=str(101+i),external_code=('SHARED-A','SHARED-B','NO-REMOTE')[i],
                    parent_code='PREVIOUS-PARENT' if i<2 else 'CURRENT-PARENT',is_variant=True,
                    is_listed=True,shop_stock=None if i==1 else 7,shop_price=29,
                    shop_availability='retain saved observation',last_push_status='ok',push_pending=False)
                  for i,pid in enumerate(self.ids)]
            db.add_all(maps); await db.flush()
            self.mapping_ids=[m.id for m in maps]
            await db.execute(text("UPDATE shop_products SET external_id='legacy-'||id, variant_code=external_code"))
            db.add(ShopProductContent(shop_id=self.shop_id,external_code='PREVIOUS-PARENT',
                    data={'product_id':800,'code':'PREVIOUS-PARENT','variants':[],
                          'stock':77,'prices':[{'price':29}],'descriptions':[{'title':'Keep the complete cached source'}]}))
            db.add(ShopProductContent(shop_id=self.shop_id,external_code='CURRENT-PARENT',
                    data={'product_id':800,'code':'CURRENT-PARENT','retained':'different unchanged cache'}))
            await db.execute(text("""INSERT INTO stock_tracking
                (product_id,warehouse_id,source_type,source_id,operator_name,archived_balance)
                SELECT :product,id,'opening','fixture','test','{"qty_on_hand":"7"}'
                FROM warehouses WHERE code='hlavny-sklad'"""),{'product':self.ids[0]})
            await db.execute(text("""INSERT INTO fifo_states (product_id,warehouse_id,activation_kind)
                SELECT :product,id,'receipt' FROM warehouses WHERE code='hlavny-sklad'"""),{'product':self.ids[0]})
            await db.execute(text("""INSERT INTO fifo_layers
                (product_id,warehouse_id,receipt_movement_id,physical_received_at,
                 quantity_original,quantity_remaining,unit_cost,cost_status,provenance)
                SELECT product_id,warehouse_id,id,now(),7,7,3.1250,'known','{"fixture":"receipt"}'
                FROM stock_movements WHERE idempotency_key='fixture-existing-receipt'"""))
            supplier=Supplier(code='parent-proof-fixture',name='Fixture supplier')
            db.add(supplier); await db.flush()
            offer=SupplierProduct(supplier_id=supplier.id,supplier_sku='SOURCE-A',name='Fixture offer',
                                  attributes={'catalog':{'variant_attributes':[]}})
            db.add(offer); await db.flush()
            db.add(ProductSupplySource(product_id=self.ids[0],supplier_product_id=offer.id,is_active=True))
        self.rename_request=self.request(product_ids=self.ids[:2],manual_matches=[
            match(pid,self.mapping_ids[i],101+i,('SHARED-A','SHARED-B')[i]) for i,pid in enumerate(self.ids[:2])])

    async def observe_remote(self, shop_code,codes,parent_ids,expected_fingerprint=None,**kwargs):
        self.assertEqual(shop_code,'xtrek'); self.assertEqual(parent_ids,['800'])
        self.assertIn('CURRENT-PARENT',codes); self.assertIn('PREVIOUS-PARENT',codes)
        return {'fingerprint':self.fingerprint,'leaves':deepcopy(self.remote),'family_leaves':deepcopy(self.family),
                'occupied_codes':[{'code':'CURRENT-PARENT','kind':'parent','native_id':'800',
                                   'parent_id':'800','parent_code':'CURRENT-PARENT'}]+[
                    {'code':v['code'],'kind':'variant','native_id':v['external_id'],
                     'parent_id':v['parent_id'],'parent_code':v['parent_code']} for v in self.remote],
                'requests':3,'observation_started_monotonic':time.monotonic()}

    async def raw_mappings(self):
        async with self.sessions() as db:
            return list((await db.execute(text('SELECT to_jsonb(sp) FROM shop_products sp ORDER BY id'))).scalars())

    async def protected(self):
        async with self.sessions() as db:
            tables=('products','product_identifiers','product_variant_attributes','product_editor_overrides',
                    'shop_product_content','supplier_products','product_supply_sources','stock_balances',
                    'stock_movements','stock_tracking','fifo_states','fifo_layers','fifo_allocations','fifo_cost_revisions',
                    'reservations','shop_order_items','stock_native_orders','stock_native_events','shop_sync_outbox')
            return {table:await db.scalar(text(f"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb) FROM {table} t"))
                    for table in tables}

    async def test_only_parent_and_trigger_timestamp_change_no_identity_or_stock_invention(self):
        before, protected=await self.raw_mappings(),await self.protected()
        preview=await self.preview(self.rename_request)
        self.assertTrue(all(v['status']=='ready' for v in preview['rows']))
        self.assertEqual(preview['rows'][1]['local_eans'],[])
        result=await self.apply(preview,self.ids[:2])
        after=await self.raw_mappings()
        for i,(old,new) in enumerate(zip(before,after)):
            expected={**old,'parent_code':'CURRENT-PARENT','updated_at':new['updated_at']} if i<2 else old
            self.assertEqual(new,expected)
            if i<2: self.assertNotEqual(old['updated_at'],new['updated_at'])
        self.assertEqual(await self.protected(),protected)
        self.assertIsNone(after[1]['shop_stock'])
        self.assertEqual(result['result']['parent_renamed_product_ids'],self.ids[:2])
        self.assertEqual(result['result']['linked_product_ids'],[])
        self.assertEqual(result['result']['mapping_before_hash'],links.digest(before))
        self.assertEqual(result['result']['mapping_after_hash'],links.digest(after))
        calls=self.observe.await_count
        self.assertEqual(await self.apply(preview,self.ids[:2]),result)
        self.assertEqual(self.observe.await_count,calls)
        self.assertEqual(await self.raw_mappings(),after)

    async def test_ordinary_preview_and_refresh_still_reject_parent_mismatch(self):
        body=self.rename_request.model_dump(mode='json')
        for item in body['manual_matches']: item.pop('reviewed_parent_rename')
        before=await self.raw_mappings()
        preview=await self.preview(ProductLinksPreviewRequest(**body))
        self.assertTrue(all(v['status']=='conflict' for v in preview['rows']))
        async with self.transaction() as db:
            with self.assertRaisesRegex(links.LinkError,'product_links_identity_changed'):
                await links.refresh(db,ProductLinksRefreshRequest(shop_code='xtrek',product_ids=self.ids[:2]))
        self.assertEqual(await self.raw_mappings(),before)

    async def test_full_stale_family_is_required_in_preview_and_apply(self):
        body=self.rename_request.model_dump(mode='json')
        body['product_ids']=body['product_ids'][:1]; body['manual_matches']=body['manual_matches'][:1]
        with self.assertRaisesRegex(links.LinkError,'product_links_parent_family_incomplete'):
            await self.preview(ProductLinksPreviewRequest(**body))
        preview=await self.preview(self.rename_request)
        with self.assertRaisesRegex(links.LinkError,'product_links_parent_family_incomplete'):
            await self.apply(preview,self.ids[:1])
        self.assertEqual((await self.apply(preview,self.ids[:2]))['status'],'completed')

    async def test_recreated_or_missing_local_native_id_is_not_a_rename(self):
        for field,value in [('external_product_id',None),('external_variant_id',None),('external_variant_id','999')]:
            async with self.transaction() as db:
                await db.execute(text(f'UPDATE shop_products SET {field}=:value WHERE id=:id'),
                                 {'value':value,'id':self.mapping_ids[0]})
            with self.subTest(field=field,value=value),self.assertRaisesRegex(links.LinkError,'product_links_parent_rename_not_allowed'):
                await self.preview(self.rename_request.model_copy(update={'request_id':uuid4()}))
            async with self.transaction() as db:
                await db.execute(text('UPDATE shop_products SET external_product_id=:parent,external_variant_id=:variant WHERE id=:id'),
                                 {'parent':'800','variant':'101','id':self.mapping_ids[0]})

    async def test_native_gtin_required_and_existing_canonical_gtin_must_agree(self):
        self.remote[1]['eans']=[]; self.family[1]['eans']=[]
        with self.assertRaisesRegex(links.LinkError,'product_links_parent_rename_ean_unverified'):
            await self.preview(self.rename_request)
        self.remote[1]['eans']=[fixture.EAN_B]; self.family[1]['eans']=[fixture.EAN_B]
        async with self.transaction() as db:
            await db.execute(text('UPDATE product_identifiers SET value=:value WHERE product_id=:id'),
                             {'value':'9780201379624','id':self.ids[0]})
        with self.assertRaises(links.LinkError):
            await self.preview(self.rename_request)

    async def test_foreign_barcode_owner_and_ambiguous_leaf_code_stay_blocked(self):
        async with self.transaction() as db:
            db.add(ProductIdentifier(product_id=self.ids[2],identifier_type=IdentifierType.ean,value=fixture.EAN_B))
        with self.assertRaisesRegex(links.LinkError,'product_links_parent_rename_owner_conflict'):
            await self.preview(self.rename_request)
        async with self.transaction() as db:
            await db.execute(text('DELETE FROM product_identifiers WHERE product_id=:id'),{'id':self.ids[2]})
        self.remote.append({**self.remote[0],'parent_id':'900','parent_code':'OTHER-PARENT','external_id':'999'})
        with self.assertRaisesRegex(links.LinkError,'ambiguous_remote_match'):
            await self.preview(self.rename_request)

    async def test_unchanged_sibling_native_set_is_frozen_and_failure_fences(self):
        preview=await self.preview(self.rename_request)
        before=await self.raw_mappings()
        self.family.append(fixture.leaf('NEW-SIBLING',104,eans=[],parent_code='CURRENT-PARENT'))
        with self.assertRaisesRegex(links.LinkError,'product_links_remote_changed') as failed:
            await self.apply(preview,self.ids[:2])
        self.assertTrue(failed.exception.known_unapplied)
        self.assertEqual(await self.raw_mappings(),before)
        self.family.pop()
        with self.assertRaisesRegex(links.LinkError,'product_links_preview_expired'):
            await self.apply(preview,self.ids[:2])

    async def test_selected_native_gtin_change_cannot_use_saved_proof(self):
        preview=await self.preview(self.rename_request)
        self.remote[1]['eans']=['9780201379624']; self.family[1]['eans']=['9780201379624']
        with self.assertRaisesRegex(links.LinkError,'product_links_remote_changed') as failed:
            await self.apply(preview,self.ids[:2])
        self.assertTrue(failed.exception.known_unapplied)

    async def test_shared_sale_mapping_cannot_use_ordinary_metadata_repair(self):
        async with self.transaction() as db:
            mapping=await db.get(ShopProduct,self.mapping_ids[0])
            mapping.shared_stock=True
            mapping.sale_barcodes=[fixture.EAN_A]
        with self.assertRaisesRegex(links.LinkError,'product_links_parent_rename_not_allowed'):
            await self.preview(self.rename_request)

    async def test_uncertain_publication_on_either_parent_blocks_preview_and_apply(self):
        from inventory_hub.product_editor_models import ProductEditorPublication
        for parent in ('PREVIOUS-PARENT','CURRENT-PARENT'):
            preview=await self.preview(self.rename_request.model_copy(update={'request_id':uuid4()}))
            publication_id=str(uuid4())
            async with self.transaction() as db:
                db.add(ProductEditorPublication(id=publication_id,product_id=self.ids[2],shop_id=self.shop_id,
                    state='uncertain',document={'identity':{'parent_code':parent}}))
            with self.subTest(parent=parent),self.assertRaisesRegex(links.LinkError,'product_links_identity_in_use') as failed:
                await self.apply(preview,self.ids[:2])
            self.assertTrue(failed.exception.known_unapplied)
            with self.assertRaisesRegex(links.LinkError,'product_links_identity_in_use'):
                await self.preview(self.rename_request.model_copy(update={'request_id':uuid4()}))
            async with self.transaction() as db:
                await db.execute(text('DELETE FROM product_editor_publications WHERE id=:id'),{'id':publication_id})

    async def test_all_local_mapping_columns_including_correct_sibling_are_frozen(self):
        for index,column in [(0,'shop_stock'),(1,'shop_price'),(2,'shop_price')]:
            preview=await self.preview(self.rename_request.model_copy(update={'request_id':uuid4()}))
            async with self.transaction() as db:
                await db.execute(text(f'UPDATE shop_products SET {column}=123 WHERE id=:id'),{'id':self.mapping_ids[index]})
            with self.subTest(index=index,column=column),self.assertRaisesRegex(links.LinkError,'product_links_parent_rename_changed') as failed:
                await self.apply(preview,self.ids[:2])
            self.assertTrue(failed.exception.known_unapplied)

    async def test_rate_limit_has_durable_unapplied_fence_and_no_business_writes(self):
        preview=await self.preview(self.rename_request); before=await self.raw_mappings()
        self.observe.side_effect=links.source.SourceError('source_rate_limited',429,60)
        with self.assertRaisesRegex(links.LinkError,'source_rate_limited') as failed:
            await self.apply(preview,self.ids[:2])
        self.assertEqual(failed.exception.retry_after,60); self.assertTrue(failed.exception.known_unapplied)
        self.assertEqual(await self.raw_mappings(),before)

    async def test_failure_after_mapping_update_rolls_back_both_rows_before_fence(self):
        preview=await self.preview(self.rename_request); before=await self.raw_mappings()
        original=rename._raw_mappings; calls=0
        async def fail_after_write(db,ids):
            nonlocal calls
            calls+=1
            result=await original(db,ids)
            if calls==2: raise links.LinkError('product_links_parent_rename_unexpected_write')
            return result
        with patch.object(rename,'_raw_mappings',side_effect=fail_after_write):
            with self.assertRaisesRegex(links.LinkError,'product_links_parent_rename_unexpected_write') as failed:
                await self.apply(preview,self.ids[:2])
        self.assertTrue(failed.exception.known_unapplied)
        self.assertEqual(await self.raw_mappings(),before)

    async def test_concurrent_same_uuid_is_applied_once_and_replays_exact_result(self):
        preview=await self.preview(self.rename_request)
        first,second=await asyncio.gather(self.apply(preview,self.ids[:2]),self.apply(preview,self.ids[:2]))
        self.assertEqual(first,second); self.assertEqual(first['status'],'completed')

    async def test_uncoordinated_mapping_writer_or_product_row_lock_fails_nowait(self):
        for lock in ('LOCK TABLE shop_products IN ROW EXCLUSIVE MODE',
                     f'SELECT id FROM products WHERE id={self.ids[0]} FOR UPDATE'):
            preview=await self.preview(self.rename_request.model_copy(update={'request_id':uuid4()}))
            async with self.sessions() as competing:
                await competing.execute(text(lock))
                try:
                    with self.subTest(lock=lock),self.assertRaisesRegex(links.LinkError,'product_links_parent_rename_busy') as failed:
                        await asyncio.wait_for(self.apply(preview,self.ids[:2]),2)
                    self.assertTrue(failed.exception.known_unapplied)
                finally:
                    await competing.rollback()

    async def test_source_freshness_is_rechecked_after_waiting_for_identity_lock(self):
        preview=await self.preview(self.rename_request)
        observed=asyncio.Event(); original=self.observe_remote
        async def notify(*args,**kwargs):
            result=await original(*args,**kwargs); observed.set(); return result
        self.observe.side_effect=notify
        async with self.sessions() as competing:
            await competing.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':fixture.IDENTITY_WRITE_LOCK})
            with patch.object(rename,'MAX_OBSERVATION_SECONDS',.02):
                task=asyncio.create_task(self.apply(preview,self.ids[:2]))
                await observed.wait(); await asyncio.sleep(.06); await competing.commit()
                with self.assertRaisesRegex(links.LinkError,'product_links_source_observation_expired') as failed:
                    await task
                self.assertTrue(failed.exception.known_unapplied)
