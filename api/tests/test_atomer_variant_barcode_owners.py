import unittest
from copy import deepcopy
from types import SimpleNamespace

from inventory_hub.db_models import IdentifierType
from inventory_hub.services import atomer_variant_barcode_owners as fix
from inventory_hub.services.atomer_review_targets import ReviewError
from inventory_hub.services.product_identity import IdentityIndex


class MisassignedSizeTests(unittest.TestCase):
    def setUp(self):
        self.large=fix.ManufacturerVariant(code='COAT-L',source_id='21',source_visible=True,size='L',supplier_code='A',manufacturer_id='901',
                                           ean='4006381333931',revision=1)
        self.xl=fix.ManufacturerVariant(code='COAT-XL',source_id='22',source_visible=False,size='XL',supplier_code='B',manufacturer_id='902',
                                        ean='9780306406157',revision=1)
        self.s=fix.Selection(run_id=4,source_sha='a'*64,admin_sha='b'*64,parent='COAT',product_id=61,variant_id=98,
            card_id=34,link_id=45,identifier_id=52,size_axis='Size',native_axes={'Size':'L'},native_visible=False,current=self.large,sibling=self.xl,
            evidence_url='https://manufacturer.example/products/coat.js',evidence_sha='c'*64)
        base={'code':'COAT-L','ean':self.xl.ean,'variant_id':98,'product_id':61,'product_code':'COAT','stock':0,
              'stocks':[{'stock_id':1,'quantity':None}],'active_yn':False,'parameters':[{'name':{'sk':'Size'},'values':[{'sk':'L'}]}]}
        self.old={'parent':{'code':'COAT','product_id':61,'variants_exists_yn':True,'descriptions':[{'title':'Coat'}],
                            'variants':[deepcopy(base)]},'variants':[base]}

    def fixed(self):
        after=deepcopy(self.old)
        for v in (after['parent']['variants'][0],after['variants'][0]):
            v['ean']=self.large.ean
        return after

    def test_one_changed_barcode_is_the_only_allowed_catalog_difference(self):
        fix.validate(self.s)
        fix.readback(self.s,self.old,self.fixed())
        for field,value in [('ean',self.xl.ean),('stock',1),('active_yn',True),('variant_id',99),
                            ('parameters',[{'name':{'sk':'Size'},'values':[{'sk':'XL'}]}])]:
            after=self.fixed()
            after['variants'][0][field]=value
            with self.subTest(field=field), self.assertRaises(ReviewError):
                fix.readback(self.s,self.old,after)
        after=self.fixed(); after['parent']['descriptions'][0]['title']='Other'
        with self.assertRaises(ReviewError):
            fix.readback(self.s,self.old,after)
        self.old['parent']['variants'][0]['stocks'][0]['quantity']=2
        with self.assertRaises(ReviewError):
            fix.projection(self.s,self.old,self.xl.ean)

    def test_separate_native_identity_must_not_appear_on_either_shop(self):
        parent=self.old['parent']
        fix.native_owners(self.s,'xtrek',[parent])
        fix.native_owners(self.s,'biketrek',[])
        extra={'product_id':18,'code':self.xl.code,'ean':self.xl.ean}
        for shop,products in [('xtrek',[parent,extra]),('biketrek',[extra]),('xtrek',[])]:
            with self.subTest(shop=shop), self.assertRaises(ReviewError):
                fix.native_owners(self.s,shop,products)

    def test_a_different_size_requires_two_distinct_manufacturer_items_and_gtins(self):
        for change in ({'manufacturer_id':self.xl.manufacturer_id},{'ean':self.xl.ean},{'size':'XL'},{'ean':'123'}):
            with self.subTest(change=change), self.assertRaises(ReviewError):
                fix.validate(self.s.model_copy(update={'current':self.large.model_copy(update=change)}))

    def test_reconciliation_uses_proof_only_while_native_and_both_cards_still_agree(self):
        doc={'selection':self.s.model_dump(mode='json'),'new_card':35}
        subjects=[{'code':v.code,'ean':self.xl.ean,'parent':'COAT'} for v in (self.large,self.xl)]
        admin={v.code:{'internal_id':v.source_id,'visibility':v.source_visible} for v in (self.large,self.xl)}
        native=[{'code':self.large.code,'ean':self.large.ean,'parent':'COAT','pid':'61','vid':'98'}]
        link=SimpleNamespace(id=45,product_id=34,external_product_id='61',external_variant_id='98',external_code=self.large.code,
                             parent_code='COAT',is_variant=True,push_pending=False)
        items=[SimpleNamespace(id=i,sku=v.code,is_active=True) for i,v in ((34,self.large),(35,self.xl))]
        ids=[SimpleNamespace(product_id=i,value=v.ean,identifier_type=IdentifierType.ean) for i,v in ((34,self.large),(35,self.xl))]
        index=IdentityIndex(6,items,(),ids)
        def lookup():
            return fix.effective_sources(subjects,admin,native,index,[link],[doc])
        result,proof=lookup()
        self.assertEqual(result[0]['ean'],self.large.ean)
        self.assertEqual(subjects[0]['ean'],self.xl.ean)
        self.assertEqual(proof[self.large.code]['sibling_card'],35)
        link.external_variant_id='99'
        self.assertFalse(lookup()[1])
        link.external_variant_id='98'
        native.append({'code':'UNRELATED','ean':self.xl.ean,'parent':'COAT','pid':'61','vid':'77'})
        self.assertFalse(lookup()[1])
