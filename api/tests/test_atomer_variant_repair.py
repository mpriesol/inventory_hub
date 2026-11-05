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
        expected=atomer_recode.protect_expected(protected,[row])
        self.assertEqual([r['ean'] for r in expected['leaves']['11']],['new','new'])
        self.assertEqual(protected['leaves']['11'][0]['ean'],'first')
        protected['leaves']['11'][0]['stock']='1'
        with self.assertRaisesRegex(ReviewError,'stock_or_ean'):
            atomer_recode.protect_expected(protected,[row])
