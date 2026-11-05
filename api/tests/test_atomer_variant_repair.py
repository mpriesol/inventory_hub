from copy import deepcopy
from unittest import TestCase
from inventory_hub.services import atomer_variant_repair as repair
from inventory_hub.services.atomer_review_targets import ReviewError


def variant(code, vid, colour):
    return dict(code=code,variant_id=vid,product_id=9,product_code='family',ean=code,stock=3,
                parameters=[{'name':{'sk':'Colour'},'values':[{'sk':colour}]}])


class SourceVariantRepair(TestCase):
    def setUp(self):
        self.a,self.b = variant('first',11,'Blue'),variant('other',12,'Black')
        self.obs = {'parent':{'code':'family','variants':[self.a,self.b]},'variants':[self.a,self.b]}
        self.row = dict(vid='11',target='first',ean='first',before={'Colour':'Blue'},values={'Size':'Large'})

    def test_selected_axis_only_and_native_recheck(self):
        old = deepcopy(self.obs)
        expected,payload = repair.desired_patch(self.obs,[self.row])
        self.assertEqual(expected['12'],{'Colour':'Black'})
        self.assertEqual(expected['11'],{'Colour':'Blue','Size':'Large'})
        item, = payload['products'][0]['variants']
        self.assertEqual(set(item),{'code','parameters'})
        self.assertEqual(self.obs,old)
        for field,value in [('target','other'),('vid','12'),('ean','changed'),('before',{'Size':'Small'})]:
            with self.assertRaises(ReviewError):
                repair.desired_patch(self.obs,[{**self.row,field:value}])

    def test_collisions_and_language_loss_blocked(self):
        with self.assertRaisesRegex(ReviewError,'collision'):
            repair.desired_patch(self.obs,[{**self.row,'values':{'Colour':' BLACK '}}])
        self.a['parameters'][0]['name']['en']='Color'
        with self.assertRaisesRegex(ReviewError,'ambiguous'):
            repair.desired_patch(self.obs,[self.row])

    def test_protected_fields_include_sibling_stock_and_parent_html(self):
        p,a = repair.projection(self.obs)
        changed = deepcopy(self.obs)
        changed['parent']['descriptions']=[{'language':'sk','long_description':'New'}]
        changed['variants'][1]['stock']=5
        self.assertNotEqual(p,repair.projection(changed)[0])
        self.assertEqual(a,repair.projection(changed)[1])


import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch
from sqlalchemy import text
import test_catalog_db as fixture


@unittest.skipUnless(fixture.TEST_URL,'Dedicated local catalog test database required')
class RepairDatabaseTest(unittest.IsolatedAsyncioTestCase):
    write_feed = fixture.CatalogDatabaseTests.write_feed
    asyncSetUp = fixture.CatalogDatabaseTests.asyncSetUp
    asyncTearDown = fixture.CatalogDatabaseTests.asyncTearDown

    async def test_intent_replay_uses_readback_and_preserves_item_note(self):
        root=Path(__file__).resolve().parents[2]/'infra/db-init'
        async with self.engine.begin() as connection:
            raw=await connection.get_raw_connection()
            for name in ['037_atomer_progress.sql','038_atomer_review.sql']:
                await raw.driver_connection.execute((root/name).read_text())
        row=dict(code='first',target_parent='family',parent='family',variant_id='11',product_id='9',hub_id=None,
                 target_code='first',target_ean='first',family_key='v:family',match='code_or_ean',reasons=['parameters_differ'])
        source=dict(code='first',parent='family',axes={'PARAM_1':'Large'},conflict=False)
        sha='c'*64
        async with self.sessions() as db:
            await db.execute(text("INSERT INTO atomer_progress_sources(sha256,source_rows,identities) VALUES(:s,1,CAST(:data AS jsonb))"),
                             dict(s=sha,data=json.dumps([source])))
            await db.execute(text("INSERT INTO atomer_progress_runs(source_sha256,checked_at,totals) VALUES(:s,now(),'{}')"),dict(s=sha))
            await db.execute(text("INSERT INTO atomer_progress_items VALUES(1,'first','variant','review','v:family',CAST(:d AS jsonb))"),dict(d=json.dumps(row)))
            await db.commit()
        plan=repair.Family(run_id=1,source_sha=sha,rows=[dict(code='first',revision=0,before={'Colour':'Blue'},names={'PARAM_1':'Size'})])
        a,b=variant('first',11,'Blue'),variant('other',12,'Black')
        old={'parent':{'code':'family','variants':[a,b]},'variants':[a,b]}
        new=deepcopy(old)
        new['variants'][0]['parameters'].append({'name':{'sk':'Size'},'values':[{'sk':'Large'}]})
        with patch.object(repair,'read_family',side_effect=[old,new]),patch.object(repair,'send') as send, \
                patch.object(repair,'target_fingerprint',return_value='a'*64),patch.object(repair,'require_target_available',AsyncMock()):
            async with self.sessions() as db:
                self.assertEqual((await repair.process(db,plan,'apply','test'))['status'],'verified')
                self.assertEqual((await repair.process(db,plan,'apply','test'))['status'],'verified')
                self.assertEqual(send.call_count,1)
                self.assertEqual(await db.scalar(text("SELECT count(*) FROM atomer_review_events")),2)
                self.assertEqual(await db.scalar(text("SELECT status FROM atomer_review_work WHERE key='first'")),'open')


class SupplierRecodeTests(TestCase):
    def test_patch_has_only_the_exact_leaf_barcode_and_checks_stock(self):
        from inventory_hub.services import atomer_recode
        a,b=variant('first',11,'Blue'),variant('other',12,'Black')
        a['stock']=0
        observed={'parent':{'code':'family','variants':[a,b]},'variants':[a,b]}
        row=dict(vid='11',target='first',ean='first',new_ean='new',before={'Colour':'Blue'})
        after,patch=repair.desired_patch(observed,[row])
        self.assertEqual(patch['products'][0]['variants'],[{'code':'first','ean':'new'}])
        protected,axes=repair.projection(observed)
        self.assertEqual(axes,after)
        updated, both = repair.desired_patch(observed,[dict(row,values={'Size':'Large'})])
        self.assertEqual(set(both['products'][0]['variants'][0]),{'code','ean','parameters'})
        self.assertEqual(updated['11']['Size'],'Large')
        expected=atomer_recode.protect_expected(protected,[row])
        self.assertEqual([r['ean'] for r in expected['leaves']['11']],['new','new'])
        self.assertEqual(protected['leaves']['11'][0]['ean'],'first')
        protected['leaves']['11'][0]['stock']='1'
        with self.assertRaisesRegex(ReviewError,'stock_or_ean'):
            atomer_recode.protect_expected(protected,[row])


class BatchReadTests(TestCase):
    def test_read_uses_exact_parent_ids_and_rejects_an_extra_leaf(self):
        from inventory_hub.services import atomer_family_batch as batch
        from unittest.mock import MagicMock
        parents=[]
        leaves=[]
        identities=[]
        for pid in (1,2):
            code=f'group-{pid}'
            leaf=dict(variant(code+'-a',pid*10,'Blue'),product_id=pid,product_code=code)
            parent=dict(code=code,product_id=pid,variants=[leaf],variants_exists_yn=True)
            parents.append(parent)
            leaves.append(leaf)
            identities.append({'parent_code':code,'product_id':pid,'variant_id':pid*10,'code':leaf['code'],'variant_code':leaf['code']})
        def page(key,rows):
            return {'current_page':1,'number_of_pages':1,'current_page_items':len(rows),'number_of_items':len(rows),key:rows}
        with patch.object(batch.transport,'_client',return_value=MagicMock()),patch.object(batch.transport,'_request',
                side_effect=[page('products',parents),page('variants',leaves)]) as get:
            self.assertEqual(set(batch.read_batch(identities,'fingerprint')),{1,2})
            self.assertTrue(all(c.kwargs['params']['product_ids']=='1;2' for c in get.call_args_list))
        with patch.object(batch.transport,'_client',return_value=MagicMock()),patch.object(batch.transport,'_request',
                side_effect=[page('products',parents),page('variants',leaves+[dict(leaves[0],variant_id=987)])]):
            with self.assertRaisesRegex(ReviewError,'changed'):
                batch.read_batch(identities,'fingerprint')


@unittest.skipUnless(fixture.TEST_URL,'Dedicated local catalog test database required')
class BatchDatabaseTest(unittest.IsolatedAsyncioTestCase):
    write_feed = fixture.CatalogDatabaseTests.write_feed
    asyncSetUp = fixture.CatalogDatabaseTests.asyncSetUp
    asyncTearDown = fixture.CatalogDatabaseTests.asyncTearDown

    async def test_two_families_share_reads_and_put_but_recover_separately(self):
        from inventory_hub.services import atomer_family_batch as batch
        from inventory_hub.services.stock_publication_source import SourceError
        root=Path(__file__).resolve().parents[2]/'infra/db-init'
        async with self.engine.begin() as conn:
            raw=await conn.get_raw_connection()
            for name in ['037_atomer_progress.sql','038_atomer_review.sql']:
                await raw.driver_connection.execute((root/name).read_text())
        sha='c'*64
        sources,items,families=[],[],[]
        old={}
        for pid in (1,2):
            par,code=f'p-{pid}',f'v-{pid}'
            vid=pid*10
            sources.append(dict(code=code,parent=par,axes={'PARAM_1':'Large'},conflict=False))
            items.append(dict(code=code,parent=par,target_parent=par,product_id=str(pid),variant_id=str(vid),
                hub_id=None,target_code=code,target_ean=code,family_key='v:'+par,match='code_or_ean',reasons=['parameters_differ']))
            leaf=dict(variant(code,vid,'Blue'),product_id=pid,product_code=par)
            old[pid]={'parent':{'code':par,'variants':[deepcopy(leaf)]},'variants':[leaf]}
            families.append(dict(run_id=1,source_sha=sha,rows=[dict(code=code,revision=0,before={'Colour':'Blue'},names={'PARAM_1':'Size'})]))
        async with self.sessions() as db:
            await db.execute(text("INSERT INTO atomer_progress_sources(sha256,source_rows,identities) VALUES(:s,2,CAST(:data AS jsonb))"),dict(s=sha,data=json.dumps(sources)))
            await db.execute(text("INSERT INTO atomer_progress_runs(source_sha256,checked_at,totals) VALUES(:s,now(),'{}')"),dict(s=sha))
            for item in items:
                await db.execute(text("INSERT INTO atomer_progress_items VALUES(1,:c,'variant','review',:f,CAST(:d AS jsonb))"),dict(c=item['code'],f=item['family_key'],d=json.dumps(item)))
            await db.commit()
        plan=batch.Pack(families=families)
        new=deepcopy(old)
        for obs in new.values(): obs['variants'][0]['parameters'].append({'name':{'sk':'Size'},'values':[{'sk':'Large'}]})
        with patch.object(batch,'read_batch',side_effect=[old,old,SourceError('timeout'),new]) as fetch, \
                patch.object(repair,'send') as send,patch.object(repair,'target_fingerprint',return_value='a'*64), \
                patch.object(repair,'require_target_available',AsyncMock()):
            async with self.sessions() as db:
                self.assertEqual((await batch.process(db,plan,'preview','test'))['status'],'ready')
                with self.assertRaises(SourceError):
                    await batch.process(db,plan,'apply','test')
                await db.rollback()
                self.assertEqual((await batch.process(db,plan,'apply','test'))['status'],'verified')
                self.assertEqual((await batch.process(db,plan,'apply','test'))['status'],'verified')
                self.assertEqual(send.call_count,1)
                self.assertEqual(len(send.call_args.args[0]['products']),2)
                self.assertEqual(fetch.call_count,4)
                self.assertEqual(await db.scalar(text("SELECT count(*) FROM atomer_review_events")),4)
