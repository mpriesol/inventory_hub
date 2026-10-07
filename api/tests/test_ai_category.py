import copy
import json
import unittest
from inventory_hub.ai_content_types import CategoryProfile, ParameterDefinition, RuleBook, Scope
from inventory_hub.services import ai_category, ai_content_provider as provider, ai_content_rules as rules
from inventory_hub.services.catalog import CatalogError
from inventory_hub.services.catalog_merchandising import category_chain
from test_ai_content import context


class AutomaticCategoryTests(unittest.TestCase):
    def setUp(self):
        self.book = RuleBook(categories=[CategoryProfile(id='general', name='General'),
            CategoryProfile(id='tyres', name='Plášte', shop_categories={'biketrek':'TYRES'},
                            parameters=[ParameterDefinition(name='Šírka plášťa')]),
            CategoryProfile(id='tubes', name='Duše', shop_categories={'biketrek':'TUBES'})])
        self.rows = [{'code':'MENU','parent_code':None,'system_root':True,'assignable':False},
            {'code':'PARTS','parent_code':'MENU','names':{'sk':'Diely'}},
            {'code':'TYRES','parent_code':'PARTS','names':{'sk':'Plášte'}},
            {'code':'ROAD','parent_code':'TYRES','names':{'sk':'Cestné'}},
            {'code':'TUBES','parent_code':'PARTS','names':{'sk':'Duše'}},
            {'code':'OFF','parent_code':'TYRES','active':False}]
        self.ctx = context(supplier='paul-lange', code='PL-1', rules_version=15)

    def prepared(self):
        ctx = copy.deepcopy(self.ctx)
        ai_category.prepare(ctx, self.book, self.rows)
        return ctx

    def test_only_leaf_candidates_and_nearest_profile_mapping(self):
        ctx = self.prepared()
        choices = ctx['classification_catalog']['choices']
        self.assertEqual({c['code'] for c in choices}, {'ROAD','TUBES'})
        road = next(c for c in choices if c['code'] == 'ROAD')
        self.assertEqual(road['profile_ids'], ['tyres'])
        self.assertEqual(road['path'], 'Diely / Plášte / Cestné')
        result = ai_category.apply(ctx, self.book, {'category_code':'ROAD','profile_id':'tyres','confident':True,'reason':'Presný typ'})
        self.assertEqual(result['resolved']['category']['parameters'][0]['name'], 'Šírka plášťa')
        self.assertEqual(result['options']['category_code'],'ROAD')
        self.assertEqual(category_chain(self.rows,'ROAD'), [{'code':'PARTS','main_yn':False},{'code':'TYRES','main_yn':False},{'code':'ROAD','main_yn':True}])
        self.assertEqual(ctx['resolved']['category']['id'], 'general', 'Original context is unchanged')

    def test_bad_or_uncertain_selection_never_reaches_content(self):
        ctx = self.prepared()
        for code, profile, confident in [('MENU','tyres',True),('TYRES','tyres',True),('invented','tyres',True),('ROAD','tubes',True),('ROAD','tyres',False),('ROAD','invented',True)]:
            with self.subTest(code=code,profile=profile,confident=confident), self.assertRaises(CatalogError):
                ai_category.apply(ctx,self.book,dict(category_code=code,profile_id=profile,confident=confident,reason='Test'))

    def test_explicit_branch_limits_automatic_selection(self):
        choices = ai_category.catalog(self.rows,self.book,'biketrek','sk','TYRES')['choices']
        self.assertEqual([c['code'] for c in choices],['ROAD'])
        with self.assertRaises(CatalogError): ai_category.catalog(self.rows,self.book,'biketrek','sk','UNKNOWN')

    def test_cycles_and_missing_ancestors_fail_before_paid_call(self):
        for rows in [[{'code':'A','parent_code':'B'},{'code':'B','parent_code':'A'}],[{'code':'A','parent_code':'missing'}]]:
            with self.assertRaises(CatalogError): ai_category.catalog(rows,self.book,'biketrek','sk')

    def test_classifier_is_bounded_and_has_no_web_or_full_rule_book(self):
        ctx = self.prepared()
        body = provider.request_body(ctx,'classification')
        self.assertNotIn('tools',body)
        self.assertFalse(body['store'])
        self.assertEqual(body['max_output_tokens'],1000)
        self.assertNotIn('rules',json.loads(body['input']))
        selected = dict(category_code='ROAD',profile_id='tyres',confident=True,reason='Test')
        result,_ = provider.parse_response({'status':'completed','output':[{'content':[{'type':'output_text','text':json.dumps(selected)}]}]},'classification')
        self.assertEqual(result,selected)
        self.assertGreater(float(ctx['estimate_usd']), float(provider.estimate(ctx)))
