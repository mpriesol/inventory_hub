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
