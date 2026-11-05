import json
import os
from pathlib import Path
import unittest
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.services import atomer_progress_display as display, atomer_progress_store as snapshots

URL=os.getenv('CATALOG_TEST_DATABASE_URL','')


@unittest.skipUnless(URL,'Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB')
class OperatorProgress(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed=urlsplit(URL)
        self.assertIn(parsed.hostname,('localhost','127.0.0.1'));self.assertTrue(parsed.path.endswith('_catalog_test'))
        self.schema='op_progress_'+uuid4().hex
        self.conn=await asyncpg.connect(URL)
        await self.conn.execute(f'CREATE SCHEMA "{self.schema}"; SET search_path TO "{self.schema}"')
        self.pg=create_async_engine(URL.replace('postgresql://','postgresql+asyncpg://',1),poolclass=NullPool,
                                    connect_args={'server_settings':{'search_path':self.schema}})
        root=Path(__file__).resolve().parents[2]/'infra'/'db-init'
        for f in ('001_schema.sql','004_shop_product_content.sql','030_shop_identity.sql','037_atomer_progress.sql','038_atomer_review.sql',
                  '040_atomer_catalog_campaign.sql','043_atomer_source_cards.sql'):
            await self.conn.execute((root/f).read_text())
        await self.conn.execute("""
          INSERT INTO shops(code,name,platform) VALUES ('xtrek','xTrek','upgates'),('biketrek','BIKETREK','upgates') ON CONFLICT DO NOTHING;
          INSERT INTO atomer_progress_sources VALUES(repeat('a',64),2,'[]');
          INSERT INTO atomer_review_exports(sha256,format,compressed) VALUES(repeat('b',64),'admin_csv','');
          INSERT INTO atomer_progress_runs(source_sha256,admin_export_sha256,checked_at,totals)
           VALUES(repeat('a',64),repeat('b',64),now(),'{"items":2}');
          INSERT INTO atomer_catalog_campaigns(id,run_id,checked_run_id,source_sha,admin_sha,shop_fingerprint,actor,selection,state)
           VALUES('campaign',1,1,repeat('a',64),repeat('b',64),'fp','operator','{}','running');
          INSERT INTO atomer_catalog_jobs(campaign,family_key,priority,leaf_count) VALUES('campaign','v:PACK',1,2);
          INSERT INTO products(id,sku,name) VALUES (71003,'TEST-LEAF-A','Sample pack');
          INSERT INTO atomer_catalog_source_cards(campaign,family_key,code,run_id,state,card_id,created,evidence)
           VALUES ('campaign','v:PACK','TEST-LEAF-A',1,'card_ready',71003,true,'{"status":"card_ready","card_id":71003,"hub_sku":"TEST-LEAF-A","source_ean":""}');
        """)
        for code in ('TEST-LEAF-A','unstarted'):
            row=dict(code=code,kind='variant',state='needs_both',card_state='missing',bike_state='no_card',bike=None,
                     hub_id=None,hub_sku=None,family_name='Sample pack',family_key='v:PACK',name='Sample pack '+code,parent='PACK',
                     candidates=[],reasons=[],ean='',product_id=None,variant_id=None,target_code=None,shop_url=None,shop_admin_url=None)
            await self.conn.execute('INSERT INTO atomer_progress_items VALUES(1,$1,\'variant\',\'needs_both\',\'v:PACK\',$2)',code,json.dumps(row))
        self.sessions=async_sessionmaker(self.pg,expire_on_commit=False)

    async def asyncTearDown(self):
        await self.pg.dispose();await self.conn.execute(f'DROP SCHEMA "{self.schema}" CASCADE');await self.conn.close()

    async def page(self, service=display, **fields):
        args=dict(kind=None,state=None,bike=None,card=None,search='',offset=0,limit=20,work='all');args.update(fields)
        async with self.sessions() as db:return await service.page(db,**args)

    async def test_only_current_registered_card_affects_filters_group_search_and_pagination(self):
        live=await self.page(state='needs_target',card='exists',bike='unlinked',search='71003')
        self.assertEqual((live['total'],live['families']),(1,1));row=live['groups'][0]['items'][0]
        self.assertEqual((row['code'],row['hub_id'],row['hub_sku'],row['state']),('TEST-LEAF-A',71003,'TEST-LEAF-A','needs_target'))
        self.assertEqual(row['catalog_status'],'card_ready');self.assertIsNone(row['product_id'])
        self.assertEqual((await self.page(snapshots,state='needs_both'))['total'],2)
        self.assertEqual((await self.page(offset=90))['groups'],[])
        self.assertEqual((await self.page(offset=90))['total'],2)
        await self.conn.execute("UPDATE products SET sku='manually edited' WHERE id=71003")
        self.assertEqual((await self.page(search='TEST-LEAF-A',card='missing'))['total'],1)
        await self.conn.execute("UPDATE products SET sku='TEST-LEAF-A'; UPDATE atomer_catalog_source_cards SET state='blocked'")
        self.assertEqual((await self.page(card='exists'))['total'],0)
        await self.conn.execute("UPDATE atomer_catalog_source_cards SET state='card_ready'; UPDATE atomer_catalog_campaigns SET admin_sha=repeat('c',64)")
        self.assertNotIn('projection',await self.page());self.assertEqual((await self.page(card='exists'))['total'],0)

    async def test_verified_shop_identities_and_links_disappear_after_manual_change(self):
        ev=dict(status='verified',card_id=71003,hub_sku='TEST-LEAF-A',ean='',xtrek={'product_id':'41','variant_id':'52','code':'TEST-LEAF-A',
                'parent':'PACK','shop_url':'https://example.test/bag','shop_admin_url':'javascript:bad'},
                biketrek={'product_id':'97','variant_id':None,'code':'BIKE-A','parent':None,'shop_url':'https://bike.test/a'})
        await self.conn.execute('''INSERT INTO atomer_catalog_proofs(campaign,family_key,code,state,evidence)
           VALUES ('campaign','v:PACK','TEST-LEAF-A','partial',$1)''',json.dumps(ev))
        for shop,pid,vid,code,parent in [('xtrek','41','52','TEST-LEAF-A','PACK'),('biketrek','97',None,'BIKE-A',None)]:
            await self.conn.execute('''INSERT INTO shop_products(shop_id,product_id,external_product_id,external_variant_id,external_code,parent_code,is_variant)
                       SELECT id,71003,$2,$3,$4,$5,$6 FROM shops WHERE code=$1''',shop,pid,vid,code,parent,bool(vid))
        a=(await self.page(state='linked',bike='linked',search='BIKE-A'))['groups'][0]['items'][0]
        self.assertEqual(a['catalog_status'],'content_pending');self.assertEqual((a['product_id'],a['variant_id']),('41','52'))
        self.assertEqual(a['shop_url'],'https://example.test/bag');self.assertIsNone(a['shop_admin_url']);self.assertEqual(a['bike']['product_id'],'97')
        await self.conn.execute("UPDATE shop_products SET external_code='changed' WHERE external_product_id='41'; UPDATE shop_products SET is_listed=false WHERE external_product_id='97'")
        self.assertEqual((await self.page(state='linked'))['total'],0)
        self.assertEqual((await self.page(bike='linked'))['total'],0)
        b=(await self.page(search='TEST-LEAF-A'))['groups'][0]['items'][0];self.assertEqual(b['state'],'review');self.assertFalse(b['shop_url'])

    async def test_original_linked_standalone_uses_empty_source_parent_and_sql_null(self):
        row=dict(code='base',kind='standalone',state='linked',card_state='exists',bike_state='unlinked',
                 hub_id=31,hub_sku='base',family_name='Standalone',family_key='s:base',name='Standalone',ean='',
                 product_id='91',variant_id=None,target_code='native-base',target_parent='',
                 shop_url='https://example.test/base',shop_admin_url=None,reasons=[],candidates=[])
        await self.conn.execute("INSERT INTO products(id,sku,name) VALUES(31,'base','Standalone')")
        await self.conn.execute("""INSERT INTO shop_products(shop_id,product_id,external_product_id,external_code,is_variant)
          SELECT id,31,'91','native-base',false FROM shops WHERE code='xtrek'""")
        await self.conn.execute("INSERT INTO atomer_progress_items VALUES(1,'base','standalone','linked','s:base',$1)",json.dumps(row))
        original=await self.page(kind='standalone',state='linked')
        self.assertEqual(original['total'],1)
        self.assertEqual(original['groups'][0]['items'][0]['shop_url'],'https://example.test/base')


    async def test_native_readback_shows_existing_target_before_link_without_claiming_completion(self):
        doc={'payload':{'code':'PACK'}}
        observed=dict(identity_verified=True,product_id=911,variants={'TEST-LEAF-A':981},
                      navigation={'TEST-LEAF-A':{'shop_url':'https://example.test/native-bag','shop_admin_url':'javascript:no'}})
        await self.conn.execute("UPDATE atomer_catalog_jobs SET stage='cards',document=$1,receipt=$2",json.dumps(doc),json.dumps(observed))
        result=await self.page(search='981',state='needs_link');self.assertEqual(result['total'],1)
        d=result['groups'][0]['items'][0];self.assertEqual((d['target_code'],d['product_id'],d['variant_id']),('TEST-LEAF-A','911','981'))
        self.assertEqual(d['shop_url'],'https://example.test/native-bag');self.assertIsNone(d['shop_admin_url'])
        self.assertEqual((await self.page(state='linked'))['total'],0)
        await self.conn.execute("UPDATE atomer_catalog_jobs SET receipt=jsonb_set(receipt,'{identity_verified}','false')")
        self.assertEqual((await self.page(search='981'))['total'],0)
        await self.conn.execute("UPDATE atomer_progress_items SET state='review',detail=detail||'{\"state\":\"review\",\"reasons\":[\"ean_differs\"]}' WHERE code='TEST-LEAF-A'")
        reviewed=(await self.page(search='TEST-LEAF-A',state='review'))['groups'][0]['items'][0]
        self.assertEqual(reviewed['reasons'],['ean_differs']);self.assertEqual(reviewed['card_state'],'exists')
        await self.conn.execute("DELETE FROM products WHERE id=71003")
        self.assertEqual((await self.page(search='TEST-LEAF-A',card='exists'))['total'],0)
