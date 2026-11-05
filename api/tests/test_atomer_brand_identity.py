import unittest
from types import SimpleNamespace as Row

from inventory_hub.db_models import IdentifierType
from inventory_hub.services import atomer_progress as tracker
from inventory_hub.services.product_identity import IdentityIndex


class SourceCardBrandTests(unittest.TestCase):
    def setUp(self):
        self.source = dict(code='MODEL',ean='4006381333931',brand='Retail',parent='',name='Protector',conflict=False,axes={})
        self.card = Row(id=4,sku='B-MODEL',brand='Maker',item_type='stock',is_active=True)
        self.identifier = Row(id=20,product_id=4,identifier_type=IdentifierType.ean,value=self.source['ean'])
        self.link = Row(id=6,product_id=4,external_code=self.card.sku,external_product_id='8',external_variant_id=None,
                        is_variant=False,parent_code=None)
        self.native = {'product_id':8,'code':self.card.sku,'ean':self.source['ean']}
        self.proof = dict(code='MODEL',source_brand='Retail',card_brand='Maker',sku='B-MODEL',id=4,product_id='8',link=6,
                          ean=self.source['ean'],native_ean=self.source['ean'])

    def run_report(self, current):
        index = IdentityIndex(2, [self.card], [], [self.identifier])
        return tracker.reconcile([self.source], [], index, [], {}, {4:[self.link]}, tracker.bike_catalog([self.native]),
                                 {'MODEL':current})[0]

    def test_separate_shop_and_card_brands_keep_same_revalidated_physical_card(self):
        self.assertEqual(self.run_report(None)['reasons'], ['card_brand_differs'])
        row = self.run_report(self.proof)
        self.assertEqual((row['state'],row['card_state'],row['bike_state'],row['hub_id']),('needs_target','exists','linked',4))
        self.assertEqual(self.card.brand,'Maker')
        self.assertEqual(self.source['brand'],'Retail')

    def test_stale_identifiers_scope_or_native_link_invalidate_receipt(self):
        for field, changed in [('link',7),('product_id','10'),('id',5),('ean','5901234123457'),
                               ('source_brand','Other'),('card_brand','Other'),('native_ean',''),('code','B')]:
            self.assertEqual(self.run_report({**self.proof,field:changed})['reasons'],['card_brand_differs'])
        self.native['ean'] = '5901234123457'
        self.assertIn('card_brand_differs',self.run_report(self.proof)['reasons'])
        self.native['ean'] = self.source['ean']
        self.source['parent'] = 'P'
        self.assertIn('card_brand_differs',self.run_report(self.proof)['reasons'])
