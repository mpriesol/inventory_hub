import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from inventory_hub.services import atomer_category_restore as restore
from inventory_hub.services import stock_publication_source as transport
from inventory_hub.services.atomer_review_targets import ReviewError


def node(native_id,parent,name,*,code=None,visible=True):
    return dict(category_id=native_id,parent_id=parent,code=code or 'C'+str(native_id),active_yn=True,
                target_category_id=None,type='siteWithProducts',show_in_menu_yn=visible,
                descriptions=[dict(language='sk',name=name)])


class Categories(unittest.TestCase):
    def setUp(self):
        self.rows=[node(1,None,'Top menu'),node(5,1,'Oblečenie')]
        self.paths=['Topánky','Topánky/Pánske','Oblečenie/Doplnky']
        self.plan=restore.Selection(root_id=1,root_code='C1',paths=self.paths)

    def decide(self,ledger=None,menu=None):
        return restore.inspect(self.plan,self.paths+['Oblečenie'],self.rows,{'Oblečenie':'Top menu/Oblečenie'},
                               {},ledger or {},menu)

    def test_levels_titles_and_menu_proof(self):
        findings=self.decide(menu={'Topánky':100})
        self.assertEqual(findings['Topánky/Pánske'][0],'waiting')
        self.assertEqual(findings['Oblečenie/Doplnky'][1]['payload']['parent_id'],5)
        self.assertFalse(findings['Oblečenie/Doplnky'][1]['payload']['show_in_menu_yn'])
        self.assertEqual(findings['Oblečenie/Doplnky'][1]['menu_basis'],'unverified_hidden_from_menu')
        state,intent=findings['Topánky'];self.assertEqual(state,'create')
        self.assertTrue(intent['payload']['show_in_menu_yn']);self.assertEqual(intent['source_menu_id'],100)
        self.rows.append(node(12,1,'Topánky',code=intent['payload']['code']))
        f=self.decide({'Topánky':{'stage':'sending','intent':intent,'ack':{'category_id':12}}})
        self.assertEqual(f['Topánky'][0],'verified');self.assertEqual(f['Topánky'][1]['origin'],'created_verified')
        self.assertEqual(f['Topánky/Pánske'][1]['payload']['parent_id'],12)
        self.rows[-1]['show_in_menu_yn']=False
        self.assertEqual(self.decide({'Topánky':{'stage':'sending','intent':intent}})['Topánky'][0],'blocked')

    def test_adopt_one_existing_do_not_touch_or_shadow_collisions(self):
        self.rows.append(node(12,1,'Topánky',visible=False))
        state,proof=self.decide()['Topánky']
        self.assertEqual((state,proof['origin'],proof['category_id']),('verified','adopted_existing',12))
        self.assertFalse(proof['show_in_menu_yn'])
        self.rows.append(node(13,5,'Iné',code=restore.stable_code('Topánky')))
        self.assertEqual(self.decide()['Topánky'][0],'blocked')
        self.rows.pop();self.rows[-1]['active_yn']=False
        self.assertEqual(self.decide()['Topánky'][0],'blocked')

    def test_lost_confirmation_never_offers_another_post_or_changed_id(self):
        ledger={'Topánky':{'stage':'sending','intent':self.decide()['Topánky'][1]}}
        self.assertEqual(self.decide(ledger)['Topánky'][0],'unconfirmed')
        self.rows.append(node(44,1,'Topánky',code=restore.stable_code('Topánky'),visible=False))
        ledger['Topánky']['ack']={'category_id':43}
        self.assertEqual(self.decide(ledger)['Topánky'][0],'blocked')

    def test_unknown_source_path_cyclic_tree_and_changed_root(self):
        with self.assertRaises(ReviewError):
            restore.inspect(self.plan,[],self.rows,{}, {},{})
        self.rows[0]['code']='other'
        with self.assertRaises(ReviewError):self.decide()
        self.rows[0]['code']='C1';self.rows[0]['parent_id']=5
        with self.assertRaises(ReviewError):self.decide()

    def test_storefront_native_hierarchy_nonclickable_visible_and_unrelated_text(self):
        html='''<div><strong>Neplatné</strong><div class="box kategorie"><ul class="kategorie0">
          <li class="cat_40"><a href="/kategoria/a"><strong>Obuv</strong></a><ul>
          <li class="cat_42 nekliknutelna"><a><strong>Dámske</strong></a></li></ul></li></ul></div></div>'''
        self.assertEqual(restore.menu_from_html(html.encode()),{'Obuv':40,'Obuv/Dámske':42})
        with self.assertRaises(ValueError):restore.menu_from_html(html.replace('cat_42','cat_40').encode())

    def test_ack_must_refer_to_unique_inserted_native_id(self):
        i={'Topánky':self.decide()['Topánky'][1]};code=i['Topánky']['payload']['code']
        row=dict(code=code,inserted_yn=True,category_id=12)
        self.assertEqual(restore.acknowledgements(i,{'categories':[row]}),{'Topánky':{'code':code,'category_id':12}})
        for bad in ([dict(row,inserted_yn=False)],[row,row],[dict(row,code='alien')],[dict(row,category_id=True)]):
            self.assertFalse(restore.acknowledgements(i,{'categories':bad}))


class FakeDB:
    def __init__(self,config):
        self.ledger={};self.commits=[];self.config=config

    async def execute(self,query,params):
        sql=str(query)
        if 'SELECT state,selection' in sql:
            data={'state':'running','selection':copy.deepcopy(self.config)}
            return SimpleNamespace(mappings=lambda:SimpleNamespace(one=lambda:data))
        if 'INSERT INTO atomer_catalog_category_restores' in sql:
            for path in params['paths']:
                self.ledger.setdefault(path,dict(path=path,stage='pending',due=True,reads=0))
        elif 'SELECT *,next_at' in sql:
            return SimpleNamespace(mappings=lambda:SimpleNamespace(all=lambda:list(self.ledger.values())))
        elif 'UPDATE atomer_catalog_category_restores' in sql:
            import json
            row=self.ledger[params['path']]
            row.update(stage=params['stage'],problem=params['problem'],due=not params['delay'],reads=row['reads']+params['observed'])
            for k in ('intent','ack','proof'):
                if params[k] is not None:row[k]=json.loads(params[k])
        elif 'UPDATE atomer_catalog_campaigns SET' in sql:
            import json
            self.config.setdefault('categories',{}).update(json.loads(params['ids']))
        else:raise AssertionError(sql)

    async def commit(self):
        self.commits.append(copy.deepcopy(self.ledger))


class DurableCategory(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plan=restore.Selection(root_id=1,root_code='C1',paths=['Topánky'])
        self.c=dict(id='c',source_sha='a',admin_sha='b',actor='operator',shop_fingerprint='test',
                    selection={'category_restore':self.plan.model_dump()})
        self.db=FakeDB(self.c['selection']);self.native=[node(1,None,'Top menu')]
        self.source=AsyncMock(return_value=SimpleNamespace(category_paths={'Topánky'}))
        self.lock=AsyncMock()

    async def run_once(self):
        return await restore.advance(self.db,self.c,self.source,self.lock)

    async def test_recent_planning_read_cannot_prove_adoption_or_authorize_a_post(self):
        fp=self.c['shop_fingerprint']='c'*64
        existing=node(21,1,'Topánky',code=restore.stable_code('Topánky'),visible=False)
        with (patch.dict(restore.mapping._REGISTRY_PAGES,clear=True),
              patch.object(restore,'current_menu',return_value={}),
              patch.object(restore,'_catalog_rows',return_value=self.native+[existing]) as live,
              patch.object(restore,'_post') as post):
            restore.mapping.remember_category_snapshot(fp,self.native)
            await self.run_once()
            post.assert_not_called();live.assert_called_once_with(fp)
            self.assertEqual(self.db.ledger['Topánky']['proof']['origin'],'observed_before_send')

    async def test_partial_loss_and_restart_verify_or_block_without_replay(self):
        def create(intents,fp):
            self.assertEqual(self.db.commits[-1]['Topánky']['stage'],'sending')
            self.native.append(node(42,1,'Topánky',code=restore.stable_code('Topánky'),visible=False))
            raise transport.SourceError('new_native_create_unconfirmed',uncertain=True,request_sent=True)
        with (patch.object(restore,'current_menu',return_value={}),
              patch.object(restore,'_catalog_rows',side_effect=lambda fp:copy.deepcopy(self.native)),
              patch.object(restore,'_post',side_effect=create) as post):
            await self.run_once();self.assertEqual(self.db.ledger['Topánky']['stage'],'sending')
            self.db.ledger['Topánky']['due']=True
            await self.run_once();self.assertFalse(await self.run_once())
            self.assertEqual(post.call_count,1)
        self.assertEqual(self.db.config['categories'],{'Topánky':42})
        self.assertEqual(self.db.ledger['Topánky']['proof']['category_id'],42)

    async def test_rate_limit_before_wire_keeps_intent_and_yields_to_cards(self):
        exc=transport.SourceError('stock_publication_rate_limited',429,retry_after=197,request_sent=False)
        with (patch.object(restore,'current_menu',return_value={}),patch.object(restore,'_catalog_rows',return_value=self.native),
              patch.object(restore,'_post',side_effect=exc)):
            self.assertFalse(await self.run_once());row=self.db.ledger['Topánky']
            self.assertEqual(row['stage'],'pending');self.assertFalse(row['due']);self.assertIn('payload',row['intent'])

    async def test_nonadmission_without_post_and_concurrently_appearing_name(self):
        snapshots=[self.native,self.native+[node(13,1,'Topánky',code='someone-else')]]
        with (patch.object(restore,'current_menu',return_value={}),patch.object(restore,'_catalog_rows',side_effect=snapshots),
              patch.object(restore,'_post') as post):
            await self.run_once();post.assert_not_called()
        self.assertEqual(self.db.ledger['Topánky']['stage'],'blocked')
