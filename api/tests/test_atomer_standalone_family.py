from copy import deepcopy
import unittest

from inventory_hub.services import atomer_standalone_family as convert, atomer_generations as gen
from inventory_hub.services.atomer_review_targets import ReviewError


class ExistingParentTests(unittest.TestCase):
    def setUp(self):
        codes={'A':'4006381333931','B':'5901234123457'}
        self.s=convert.Family(run_id=1,source_sha='1'*64,admin_sha='2'*64,parent='SOURCE',native_code='37',
            product_id=39,card_id=55,link_id=19,prior_leaf='A',observed_stock=1,
            members=[convert.Member(code=c,ean=gtin,source_id=str(i+700),revision=0) for i,(c,gtin) in enumerate(codes.items())])
        self.warehouse=[{'stock_id':1,'quantity':None,'position':None,'code':None}]
        self.old=dict(product_id=39,code='37',ean=codes['A'],active_yn=True,stock=1,stocks=self.warehouse,variants=[],variants_exists_yn=False,
                      variants_stock=None,manufacturer='Maker',descriptions=[{'language':'sk','seo_url':'original'}])
        variants=[dict(code=c,ean=g,parameters=gen.parameters({'Size':c}),main_yn=c=='A',active_yn=True,
                       weight=12,prices=[]) for c,g in codes.items()]
        self.doc={'payload':{'code':'SOURCE','manufacturer':'Maker','variants':variants,'vats':{'sk':23}},'flags':{'A':[],'B':[]}}
        self.after={**deepcopy(self.old),'variants_exists_yn':True,'variants_stock':1,'variants':[
            {**v,'product_id':39,'product_code':'37','variant_id':i+10,'stock':1 if i==0 else None,'stocks':self.warehouse,
             'parameters_new':v['parameters'],'labels':[],'images':[]} for i,v in enumerate(variants)]}

    def test_old_quantity_stays_with_same_gtin_and_native_parent(self):
        patch=convert.structure(self.s,self.doc,self.old)
        self.assertEqual(patch['code'],'37')
        self.assertEqual([v.get('stock','omitted') for v in patch['variants']],[1,'omitted'])
        self.assertNotIn('stock',self.doc['payload']['variants'][0])
        ids,receipt=convert.observed(self.s,self.old,self.after,self.after['variants'],self.doc)
        self.assertEqual(ids,{'A':10,'B':11})
        self.assertIsNone(receipt['B']['stock'])
        self.assertEqual(receipt['A']['stocks'][0]['quantity'],None)

    def test_quantity_cannot_be_copied_onto_siblings_or_inferred(self):
        for key,row,bad in [('stock',0,None),('stock',1,1),('ean',0,'5901234123457'),('product_code',1,'changed')]:
            altered=deepcopy(self.after); altered['variants'][row][key]=bad
            with self.assertRaises(ReviewError):
                convert.observed(self.s,self.old,altered,altered['variants'],self.doc)
        flat=deepcopy(self.after['variants']);flat[0]['stock']=None
        with self.assertRaises(ReviewError):
            convert.observed(self.s,self.old,self.after,flat,self.doc)
        before={**self.old,'stock':None}
        with self.assertRaises(ReviewError):
            convert.structure(self.s,self.doc,before)

    def test_other_catalog_owner_or_changed_original_seo_blocks(self):
        convert.free_other_codes(self.s,[self.old])
        with self.assertRaises(ReviewError):
            convert.free_other_codes(self.s,[self.old,{'code':'outside','product_id':40,'ean':'5901234123457'}])
        amended=deepcopy(self.after);amended['descriptions'][0]['seo_url']='other'
        with self.assertRaises(ReviewError):
            convert.observed(self.s,self.old,amended,amended['variants'],self.doc)
        self.old['stocks'][0]['quantity']=2
        with self.assertRaises(ReviewError):
            convert.structure(self.s,self.doc,self.old)

    def test_only_unedited_exact_machine_queue_or_its_own_undelivered_claim(self):
        detail={'code':'B','card_state':'missing','state':'needs_both','reasons':[],'bike_state':'no_card'}
        instr='\n'.join(convert.proposals.INSTRUCTIONS[k] for k in convert.proposals.suggestions(detail,'default'))
        w={'revision':1,'status':'queued','instruction':instr,'scope':'default'}
        group={'scope':'default'}
        self.assertTrue(convert.can_adopt(w,1,'approve_suggestions',detail,group,None,{1:'approve_suggestions'}))
        self.assertFalse(convert.can_adopt(w,1,'save',detail,group,None,{}))
        self.assertFalse(convert.can_adopt({**w,'instruction':'My instruction'},1,'approve_suggestions',detail,group,None,{}))
        owned={'stage':'blocked','guards':{'own':[['item','B',2]]},'receipt':{}}
        history={1:'approve_suggestions',2:'claim',3:'block'}
        self.assertTrue(convert.can_adopt({**w,'revision':2,'status':'working'},2,'claim',detail,group,owned,history))
        self.assertTrue(convert.can_adopt({**w,'revision':3,'status':'blocked'},3,'block',detail,group,owned,history))
        self.assertFalse(convert.can_adopt({**w,'revision':2,'status':'working'},2,'claim',detail,group,
                                         {**owned,'receipt':{'dispatched':True}},history))
        self.assertFalse(convert.can_adopt({**w,'revision':3,'status':'blocked'},3,'block',detail,group,
                                         {**owned,'guards':{'own':[['item','other',2]]}},history))

        self.assertFalse(convert.can_adopt({**w,'revision':2,'status':'working'},2,'claim',detail,group,owned,
                                         {1:'save',2:'claim'}))
