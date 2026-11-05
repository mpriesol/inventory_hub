import json
import os
from copy import deepcopy
from pathlib import Path
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from current_schema_fixture import apply_current_core_schema
from inventory_hub.db_models import IdentifierType, Product, ProductIdentifier
from inventory_hub.db_models_ext import ShopProduct
from inventory_hub.services import atomer_card_batch as batch
from inventory_hub.services import atomer_catalog_checkpoint as checkpoint, atomer_source_cards as source_cards


GTIN = '5901234123457'
OTHER = '4006381333931'


def parent(code,pid,variants):
    return dict(code=code,product_id=pid,variants_exists_yn=bool(variants),set_yn=False,
                descriptions=[dict(language='sk',title=code)],manufacturer='Example',
                variants=[dict(code=c,variant_id=v,product_id=pid,product_code=code,ean=e,parameters=[])
                          for c,v,e in variants])


def item(code='AT-CODE',ean=GTIN):
    return dict(code=code,name=code,ean=ean,parent='AT-PARENT',family_key='v:AT-PARENT',kind='variant',
                state='needs_both',reasons=[],hub_id=None,hub_sku=None,product_id=None,variant_id=None,
                target_code=None,target_parent=None,target_ean=None)


class CardIdentityTest(unittest.TestCase):
    def test_cross_shop_alias_with_same_gtin_in_different_family(self):
        target=parent('OTHER-FAMILY',900,[('BIKE-123',81,GTIN)])
        catalog=batch.Catalog.build(1,[target])
        self.assertEqual(batch.choose_bike(item(),catalog),('900','81'))

    def test_contradictory_code_and_barcode_and_duplicate_gtin_are_held(self):
        for first in [OTHER,GTIN]:
            target=parent('POSITIONS',900,[('AT-CODE',81,first),('BIKE-ALT',82,GTIN)])
            with self.assertRaisesRegex(batch.CardConflict,'multiple_native_candidates'):
                batch.choose_bike(item(),batch.Catalog.build(1,[target]))

    def test_till_sibling_duplicate_only_blocks_its_physical_owners(self):
        till=parent('TILL',900,[('CLEAR',81,GTIN),('DUPE-A',82,OTHER),('DUPE-B',83,OTHER)])
        # The technical parent also repeats the clean sellable's barcode.
        till['ean']=GTIN
        catalog=batch.Catalog.build(1,[till])
        self.assertIn('remote_barcode_duplicate',catalog.families[0]['reasons'])
        self.assertEqual(batch.choose_bike(item('CLEAR'),catalog),('900','81'))
        for code in ['82','83']:
            with self.assertRaisesRegex(batch.CardConflict,'remote_barcode_duplicate'):
                catalog.get(('900',code))

    def test_duplicate_in_other_parent_also_holds_both_selected_owners(self):
        first=parent('TILL',900,[('FIRST',81,GTIN)])
        second={**parent('OTHER',7,[]),'ean':GTIN}
        catalog=batch.Catalog.build(1,[first,second])
        for key in [('900','81'),('7',None)]:
            with self.assertRaisesRegex(batch.CardConflict,'remote_barcode_duplicate'):
                catalog.get(key)

    def test_large_till_reads_only_the_selected_codes_and_parent_header(self):
        large=parent('TILL',900,[(f'SKU-{i}',i,GTIN if i==83 else '') for i in range(1,110)])
        large['last_update_time']='2026-01-01T12:00:00+01:00'
        native={**large['variants'][82],'parameters':[{'name':'Model','value':'A'}]}
        lookups=[]
        def read(client,path,key,filters):
            lookups.append((path,filters))
            return [{**large,'variants':[]}] if path=='products' else [native]
        with patch.object(batch,'_client',return_value=Mock()),patch.object(batch,'target_fingerprint',return_value='f'), \
                patch('inventory_hub.services.atomer_family_batch.collect_pages',side_effect=read),patch.object(batch,'_selected') as full:
            parents,observed=batch._read_candidates('biketrek',batch.Catalog.build(1,[large]),{('900','83')})
        full.assert_not_called()
        self.assertEqual(lookups,[('products',{'product_ids':'900','variants_yn':'false'}),
                                  ('products/variants',{'variant_codes':'SKU-83'})])
        self.assertEqual(observed,{('900','83')})
        self.assertEqual(len(parents['900']['variants']),109)
        self.assertEqual(parents['900']['variants'][82],native)

    def test_missing_ean_cannot_change_parent_or_sellable_kind_by_sku_alone(self):
        self.assertIsNone(batch.choose_bike(item(ean=''),batch.Catalog.build(1,[])))
        target=parent('TILL',900,[('AT-CODE',81,'')])
        with self.assertRaisesRegex(batch.CardConflict,'exact_identity_required'):
            batch.choose_bike(item(ean=''),batch.Catalog.build(1,[target]))

    def test_both_and_default_never_lift_operator_single_shop_exception(self):
        for req in ['both','default',None]:
            self.assertEqual(batch.effective_scope({'scope':'default'},{'scope':'xtrek'},req),'xtrek')
        with self.assertRaisesRegex(batch.CardConflict,'scope_changed'):
            batch.effective_scope({'scope':'default'},{'scope':'xtrek'},'biketrek')
        with self.assertRaisesRegex(batch.CardConflict,'closed_or_blocked'):
            batch.effective_scope({'status':'done'},{'status':'open'},'both')

    def test_same_native_id_changed_kind_or_code_is_not_a_target(self):
        family=parent('AT-PARENT',81,[('CHANGED',82,GTIN)])
        with self.assertRaisesRegex(batch.CardConflict,'code_or_kind_changed'):
            batch.choose_x(item(),batch.Catalog.build(2,[family]),(81,82))

    def test_batch_rejects_both_claimants_of_one_card(self):
        plans={'ONE':{'card':42,'keys':{'xtrek':('1',None)}},'TWO':{'card':42,'keys':{'xtrek':('2',None)}}}
        results={}
        batch._reject_collisions(plans,results)
        self.assertEqual(plans,{})
        self.assertEqual({v['status'] for v in results.values()},{'blocked'})

    def test_placeholder_is_sku_only_and_original_is_recorded(self):
        result=batch.barcode_subject(item('A-123','A-123'))
        self.assertEqual(result['ean'],'')
        self.assertEqual(result['barcode_evidence'],{'before':'A-123','type':'source_repeats_sku'})
        with self.assertRaisesRegex(batch.CardConflict,'source_ean_unverified'):
            batch.barcode_subject(item('A-123','bad'))

    def test_independent_barcode_proof_is_pinned_to_source_and_parent(self):
        proof=dict(before='missing_check_digit',parent='AT-PARENT',ean=OTHER,
                   manufacturer_or_supplier='https://example.org/workbook',
                   evidence='Exact manufacturer model and size, published GTIN.')
        self.assertEqual(batch.barcode_subject(item(ean=proof['before']),proof)['ean'],OTHER)
        for src in [item(ean='other-invalid'),item(ean=GTIN),{**item(ean=proof['before']),'parent':'CHANGED'}]:
            with self.assertRaisesRegex(batch.CardConflict,'evidence_changed'):
                batch.barcode_subject(src,proof)
        with self.assertRaisesRegex(batch.CardConflict,'evidence_changed'):
            batch.barcode_subject(item(ean=''),{**proof,'before':''})
        blank={**proof,'before':'','required_atomer_id':'123'}
        self.assertEqual(batch.barcode_subject(item(ean=''),blank,source_id='123')['ean'],OTHER)
        for wrong in [None,'0123','124']:
            with self.assertRaisesRegex(batch.CardConflict,'source_id_changed'):
                batch.barcode_subject(item(ean=''),blank,source_id=wrong)

    def test_single_zero_can_only_be_omitted_for_a_source_pinned_standalone(self):
        row={**item(ean='0'),'parent':None,'kind':'standalone','family_key':'s:AT-CODE'}
        result=batch.barcode_subject(row,zero_id='123')
        self.assertEqual(result['ean'],'')
        self.assertEqual(result['barcode_evidence']['native_id'],'123')
        for mismatch in [dict(kind='variant'),dict(ean='00'),dict(family_key='s:OTHER')]:
            with self.assertRaisesRegex(batch.CardConflict,'subject_changed'):
                batch.barcode_subject({**row,**mismatch},zero_id='123')

    def test_existing_hub_gtin_finds_bike_when_source_has_only_a_different_sku(self):
        target=parent('LEGACY',7,[('OLD-123',8,GTIN)])
        catalog=batch.Catalog.build(1,[target])
        self.assertEqual(batch.choose_bike(item(ean=''),catalog,card_eans=(GTIN,)),('7','8'))

    def test_source_only_needs_full_archived_identity_and_visibility(self):
        row={**item(),'source_state':dict(product_origin='admin_csv',product=True,variant_origin='admin_csv',variant=False)}
        source=dict(code=row['code'],parent=row['parent'],native_id='123',ean=GTIN,sku_owners=[row['code']],
                    gtin_owners=[row['code'],row['parent']],product_active=True,variant_active=False)
        source_cards.physical_subject(row,source)
        for patch_ in [dict(gtin_owners=['sibling']),dict(native_id=None),dict(variant_active=True)]:
            with self.assertRaises(batch.CardConflict):
                source_cards.physical_subject(row,{**source,**patch_})

    def test_broken_catalog_scan_resumes_without_publishing_partial_index(self):
        parents=[dict(product_id=i+1,code=f'P{i}',creation_time='2020-01-01T00:00:00+00:00')
                 for i in range(205)]
        pages=[]
        def read(client,path,params):
            changed='last_update_time_from' in params
            n,limit=params['page'],params['current_page_items']
            source=[] if changed else parents
            if not changed and limit==100:
                pages.append(n)
                if pages==[1,2]:
                    raise checkpoint.export.ExportError('upgates_export_budget_deferred')
            rows=source[(n-1)*limit:n*limit]
            return dict(products=rows,number_of_items=len(source),number_of_pages=(len(source)+limit-1)//limit,
                        current_page=n,current_page_items=len(rows))
        with TemporaryDirectory() as tmp,patch.object(checkpoint.export,'_read',side_effect=read):
            directory=Path(tmp)
            with self.assertRaisesRegex(checkpoint.export.ExportError,'budget_deferred'):
                checkpoint.collect(None,directory,'a'*64)
            self.assertEqual(checkpoint.collect(None,directory,'a'*64),parents)
            self.assertEqual(pages,[1,2,2,3])
            self.assertEqual(checkpoint.collect(None,directory,'a'*64),parents)
            self.assertEqual(pages,[1,2,2,3])


TEST_URL=os.environ.get('CATALOG_TEST_DATABASE_URL','')


@unittest.skipUnless(TEST_URL,'isolated localhost *_catalog_test database required')
class AtomerCardDatabaseTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url=urlsplit(TEST_URL)
        if url.hostname not in ('localhost','127.0.0.1') or not url.path.endswith('_catalog_test'):
            raise RuntimeError('Refusing a non-test database')
        self.schema='atomer_cards_'+uuid4().hex
        root=Path(__file__).resolve().parents[2]/'infra'/'db-init'
        connection=await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            for name in ('001_schema.sql','004_shop_product_content.sql','007_order_stock.sql','011_fifo.sql',
                         '012_product_editor.sql','014_supplier_availability.sql','019_stock_tracking.sql',
                         '029_sku_policy.sql','037_atomer_progress.sql','038_atomer_review.sql',
                         '040_atomer_catalog_campaign.sql','043_atomer_source_cards.sql'):
                await connection.execute((root/name).read_text())
            await apply_current_core_schema(connection)
            await connection.execute("INSERT INTO atomer_progress_sources(sha256,source_rows,identities) VALUES(repeat('a',64),1,'[]')")
            await connection.execute("INSERT INTO atomer_progress_runs(source_sha256,checked_at,totals) VALUES(repeat('a',64),now(),'{}')")
            for code,ean in [('AT-CODE',GTIN),('NEW-CODE',OTHER)]:
                await connection.execute("""
                    INSERT INTO atomer_progress_items(run_id,code,kind,state,family_key,detail)
                    VALUES(1,$1,'variant','needs_both','v:AT-PARENT',$2::jsonb)
                """,code,json.dumps(item(code,ean)))
            await connection.execute("""INSERT INTO atomer_review_work(kind,key,revision,status,scope,decision,note,instruction,result,updated_by)
                VALUES ('family','v:AT-PARENT',2,'working','default','undecided','','create xTrek family','','operator')""")
        finally:
            await connection.close()
        self.engine=create_async_engine(TEST_URL.replace('postgresql://','postgresql+asyncpg://',1),poolclass=NullPool,
                                connect_args={'server_settings':{'search_path':self.schema}})
        self.sessions=async_sessionmaker(self.engine,expire_on_commit=False,autoflush=False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        connection=await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            await connection.close()

    async def test_preserves_existing_card_and_links_new_leaf_in_both_shops_once(self):
        catalogs={'xtrek':[parent('AT-PARENT',71,[('AT-CODE',11,GTIN),('NEW-CODE',12,OTHER)])],
                  'biketrek':[parent('BIKE-PARENT',72,[('LEGACY-BIKE',21,GTIN),('BIKE-NEW',22,OTHER)])]}
        def selected(s,ids):
            return {str(r['product_id']):deepcopy(r) for r in catalogs[s] if str(r['product_id']) in ids}
        async with self.sessions() as db:
            card=Product(sku='LEGACY-BIKE',name='Original label',brand='Example')
            db.add(card);await db.flush()
            db.add(ProductIdentifier(product_id=card.id,identifier_type=IdentifierType.ean,value=GTIN,is_primary=True))
            shops=dict((await db.execute(text('SELECT code,id FROM shops'))).all())
            db.add(ShopProduct(shop_id=shops['biketrek'],product_id=card.id,external_code='LEGACY-BIKE',
                               external_product_id='72',external_variant_id='21',parent_code='BIKE-PARENT',is_variant=True))
            await db.commit()
            with patch.object(batch,'_selected',side_effect=selected):
                args=dict(actor='operator',catalogs=catalogs,include_managed=True,
                          native_ids={'AT-CODE':(71,11),'NEW-CODE':(71,12)})
                first=await batch.reconcile(db,1,['AT-CODE','NEW-CODE'],apply=True,**args)
                await db.commit()
                again=await batch.reconcile(db,1,['AT-CODE','NEW-CODE'],apply=True,**args)
                await db.commit()
            self.assertEqual({c['status'] for c in first.values()},{'verified'})
            self.assertEqual(first['NEW-CODE']['card_action'],'created')
            self.assertEqual(again['NEW-CODE']['card_action'],'reused')
            self.assertEqual({k:{**v,'card_action':'reused'} for k,v in first.items()},again)
            self.assertEqual(first['AT-CODE']['card_id'],card.id)
            self.assertEqual(first['AT-CODE']['hub_sku'],'LEGACY-BIKE')
            self.assertTrue(all(first[key][s]['link_id'] for key in first for s in batch.SHOPS))
            self.assertEqual(first['NEW-CODE']['biketrek']['variant_id'],'22')
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM products')),2)
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM stock_movements')),0)
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM stock_balances')),0)
            self.assertEqual(await db.scalar(select(Product.name).where(Product.id==card.id)),'Original label')

    async def test_source_cards_without_upgates_create_once_and_write_no_physical_stock_or_link(self):
        code,ean='NEW-CODE',OTHER
        row=item(code,ean)
        row['source_state']=dict(product_origin='admin_csv',product=True,variant_origin='admin_csv',variant=False)
        archived=dict(code=code,parent='AT-PARENT',name='Archived hidden leaf',brand='Example',weight_g=45,
                      native_id='490',sku_owners=[code],gtin_owners=[code],ean=ean,product_active=True,variant_active=False)
        async with self.sessions() as db:
            await db.execute(text('UPDATE atomer_progress_items SET detail=CAST(:row AS jsonb) WHERE code=:c'),
                            dict(row=json.dumps(row),c=code))
            details={code:{**row,'source_card':archived}}
            first=(await batch.source_cards(db,1,details,actor='operator'))[code]
            await db.commit()
            again=(await batch.source_cards(db,1,details,actor='operator'))[code]
            self.assertEqual((first['status'],first['action'],first['gtins']),('card_ready','created',[ean]))
            self.assertEqual((again['action'],again['card_id']),('reused',first['card_id']))
            for table,count in [('products',1),('shop_products',0),('stock_movements',0),('stock_balances',0)]:
                self.assertEqual(await db.scalar(text(f'SELECT count(*) FROM {table}')),count)

    async def test_needs_card_requires_exact_independent_cached_native_target(self):
        code='NEW-CODE'
        row={**item(code,OTHER),'state':'needs_card','product_id':'718','variant_id':'98','target_parent':'AT-PARENT',
             'target_code':code,'target_ean':OTHER,'source_state':dict(product_origin='admin_csv',product=True,
                                                                      variant_origin='admin_csv',variant=False)}
        source=dict(code=code,parent='AT-PARENT',name='Preserved name',native_id='396',sku_owners=[code],gtin_owners=[code],
                    ean=OTHER,product_active=True,variant_active=False)
        cached=parent('AT-PARENT',718,[(code,98,OTHER)]); cached['set_yn']=False
        async with self.sessions() as db:
            await db.execute(text('UPDATE atomer_progress_items SET detail=CAST(:r AS jsonb) WHERE code=:c'),
                             dict(r=json.dumps(row),c=code))
            async def check():
                return (await batch.source_cards(db,1,{code:{**row,'source_card':source}},actor='op'))[code]
            with patch.object(batch,'_client',side_effect=AssertionError('unexpected remote call')):
                self.assertEqual((await check())['reason'],'source_card_cached_target_missing_or_ambiguous')
                cached['variants'][0]['variant_id']=77
                await db.execute(text("INSERT INTO shop_product_content(shop_id,external_code,data) "
                                     "VALUES ((SELECT id FROM shops WHERE code='xtrek'),'AT-PARENT',CAST(:p AS jsonb))"),
                                 dict(p=json.dumps(cached)))
                self.assertEqual((await check())['reason'],'source_card_cached_target_not_exact')
                cached['variants'][0]['variant_id']=98
                await db.execute(text("UPDATE shop_product_content SET data=CAST(:p AS jsonb)"),dict(p=json.dumps(cached)))
                ready=await check()
            self.assertEqual((ready['status'],ready['created'],ready['gtins']),('card_ready',True,[OTHER]))
            self.assertEqual(ready['cached_native_target']['variant_id'],'98')
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM shop_products')),0)

    async def test_later_manufacturer_ean_only_enriches_generated_card_with_its_prior_receipt(self):
        code='NEW-CODE'
        row=item(code,'')
        row['source_state']=dict(product_origin='admin_csv',product=True,variant_origin='admin_csv',variant=False)
        archived=dict(code=code,parent='AT-PARENT',name='Leaf',native_id='490',sku_owners=[code],gtin_owners=[],
                      corrected_gtin_owners=[],ean='',product_active=True,variant_active=False)
        proof=dict(before='',parent='AT-PARENT',required_atomer_id='490',ean=OTHER,
                   manufacturer_or_supplier='https://example.org/source',evidence='Manufacturer reports the exact SKU, size and GTIN.')
        async with self.sessions() as db:
            await db.execute(text('UPDATE atomer_progress_items SET detail=CAST(:row AS jsonb) WHERE code=:c'),
                             dict(row=json.dumps(row),c=code))
            inputs={code:{**row,'source_card':archived}}
            async def again(proofs=None):
                return (await batch.source_cards(db,1,inputs,actor='op',barcode_corrections=proofs))[code]
            first=await again()
            self.assertTrue(first['created']); self.assertEqual(first['gtins'],[])
            self.assertEqual((await again({code:proof}))['reason'],'existing_card_gtin_requires_verification')
            await db.execute(text("""INSERT INTO atomer_catalog_campaigns(id,run_id,source_sha,admin_sha,shop_fingerprint,actor,selection,state)
                VALUES ('proof',1,'s','a','f','op','{}','running')"""))
            await db.execute(text("""INSERT INTO atomer_catalog_jobs(campaign,family_key,priority,leaf_count)
                VALUES ('proof','v:AT-PARENT',1,1)"""))
            await db.execute(text("""INSERT INTO atomer_catalog_source_cards(campaign,family_key,code,run_id,state,card_id,created,evidence)
                VALUES ('proof','v:AT-PARENT',:c,1,'card_ready',:p,true,CAST(:v AS jsonb))"""),
                dict(c=code,p=first['card_id'],v=json.dumps(first)))
            fresh=await again({code:proof}); self.assertEqual(fresh['gtin_added'],OTHER)
            self.assertEqual((fresh['card_id'],fresh['gtins'],fresh['created']),(first['card_id'],[OTHER],False))
            self.assertNotIn('gtin_added',await again({code:proof}))
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM product_identifiers')),1)
