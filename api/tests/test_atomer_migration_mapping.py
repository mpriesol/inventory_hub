import unittest
from unittest.mock import patch

from inventory_hub.services import atomer_migration_mapping as mapping
from inventory_hub.services.atomer_review_targets import ReviewError


def category(i, name, parent=None, **options):
    return dict(category_id=i, parent_id=parent, descriptions=[{'language':'sk','name':name}],
                active_yn=True,type='siteWithProducts',**options)


class CategoryMappingTest(unittest.TestCase):
    def test_complete_paths_and_renamed_parent_keep_departments(self):
        cats = [category(1,'Menu'),category(2,'Adults',1),category(3,'Junior',1),
                category(4,'Packs',2),category(5,'Packs',3),category(6,'Small',4)]
        choices, issues = mapping.category_options(cats,['Packs','Packs/Small','Children/Packs'],
                                                   {'Children':'Menu/Junior'})
        self.assertEqual(choices,{'Packs/Small':6,'Children/Packs':5})
        self.assertEqual(issues,{'Packs':'ambiguous_category'})

    def test_single_generic_category_requires_reviewed_department(self):
        cats=[category(1,'Menu'),category(2,'Junior',1),category(3,'Shoes',2)]
        self.assertEqual(mapping.category_options(cats,['Shoes']),
                         ({},{'Shoes':'category_root_unconfirmed'}))
        self.assertEqual(mapping.category_options(cats,['Shoes'],{'Shoes':'Menu/Junior/Shoes'}),
                         ({'Shoes':3},{}))

    def test_broad_secondary_is_pinned_to_the_primary_department(self):
        cats = [category(1,'Menu'),category(2,'Women',1),category(3,'Coats',2),
                category(4,'Warm',3),category(5,'Men',1),category(6,'Coats',5)]
        resolved = {'Clothes/Women':3,'Clothes/Women/Warm':4,'Clothes/Men':6}
        self.assertEqual(mapping.category_contexts(cats,resolved,resolved,['Clothes']),
                         {'Clothes/Women/Warm':{'Clothes':3},'Clothes/Women':{'Clothes':3},'Clothes/Men':{'Clothes':6}})
        conflicting = mapping.category_contexts(cats,resolved,dict(resolved,**{'Clothes/Women':6}),['Clothes'])
        self.assertNotIn('Clothes/Women/Warm',conflicting)
        named = {'Coats/Warm':4}
        self.assertEqual(mapping.category_contexts(cats,named,named,['Coats']),
                         {'Coats/Warm':{'Coats':3}})

    def test_invalid_hierarchy_and_reference_type_cannot_be_selected(self):
        with self.assertRaises(ReviewError):
            mapping.category_options([category(1,'Menu',2),category(2,'Odd',1)],['Menu'])
        entry = category(1,'Menu')
        link = dict(category(2,'Redirect',1),type='linkCategory',target_category_id=3)
        self.assertEqual(mapping.category_options([entry,link], ['Menu/Redirect'])[0], {})


class ParameterEvidenceTest(unittest.TestCase):
    def test_conflict_and_duplicate_codes_never_become_mappings(self):
        def source(code, value):
            return dict(code=code,ean='123'+code,parent='P',axes={'PARAM_10':value,'PARAM_11':value+'!'})

        def native(src,n1,n2):
            a=src['axes']
            return dict(code=src['code'],ean=src['ean'],parent='P',
                        axes={n1:[a['PARAM_10']],n2:[a['PARAM_11']]})

        rows=[source(str(i),v) for i,v in enumerate(('Red','Blue','Grey'))]
        observations=[native(r,'Colour','Dimensions' if i!=1 else 'Size') for i,r in enumerate(rows)]
        self.assertEqual(mapping.infer_axes(rows,observations), {'PARAM_10':'Colour'})
        self.assertEqual(mapping.infer_axes(rows,observations+[observations[0]]), {})
        self.assertEqual(mapping.infer_axes(rows,[*observations[:2],dict(observations[2],ean='wrong')]),{})

    def test_exact_source_title_can_anchor_an_atomer_named_feed_without_ean(self):
        rows=[{'code':c,'name':'P / '+c,'axes':{'PARAM_2':c}} for c in 'abc']
        feed=[{'code':r['code'],'name':r['name'],'axes':{'Colour':r['code']}} for r in rows]
        self.assertEqual(mapping.infer_axes(rows, [], feed), {'PARAM_2':'Colour'})


class NativeRegistryTest(unittest.TestCase):
    def test_names_are_locale_specific_unique_and_system_flags_use_type(self):
        def avail(i,sk):
            return {'id':i,'descriptions':[{'language':'sk','name':sk},{'language':'cs','name':'Skladem'}]}
        labels=[{'label_id':4,'type':'action','descriptions':[{'language_id':'sk','name':'Ponuka'}]},
                {'label_id':5,'type':'custom','descriptions':[{'language_id':'sk','name':'Odporúčané'}]}]
        self.assertEqual(mapping.registry_options([avail(1,'Skladom'), avail(2,'Iné'),avail(3,'iné')],labels),
                         {'availability':{'skladom':1},'labels':{'Akcia':4,'Odporúčaný':5}})
        duplicate = dict(labels[0],label_id=6)
        self.assertNotIn('Akcia', mapping.registry_options([], [*labels,duplicate])['labels'])

    @patch.object(mapping.transport,'_request')
    @patch.object(mapping.batch,'collect_pages')
    @patch.object(mapping.publication,'_options',return_value={'prices_with_vat':True,'pricelist':'Default'})
    def test_empty_filtered_availability_can_be_verified_by_native_id(self, _, collect, request):
        collect.side_effect=[[],[],[],[],[],[]]
        request.return_value={'availabilities':[{'id':19,'descriptions':[{'language':'sk','name':'Na ceste'}]}]}
        result=mapping.read_registry(object(),availability_ids=[19])
        self.assertEqual(result['availability'],{'na ceste':19})


class RegistryAccessTest(unittest.TestCase):
    def test_quota_between_pages_retains_only_completed_planning_reads(self):
        fingerprint='e'*64
        ids=iter((1,2))
        seen=[]
        def send(client,method,path,*,params=None):
            page=(params or {}).get('page',1)
            seen.append((path,page))
            if path=='categories' and page==2 and next(ids)==1:
                raise mapping.transport.SourceError('stock_publication_rate_limited',429,retry_after=61)
            if path=='config':return {'config':{'prices_with_vat_yn':True}}
            if path=='languages':return {'languages':[{'language_id':'sk','active_yn':True,'currency_id':'EUR'}]}
            if path=='pricelists':return {'pricelists':[{'default_yn':True,'name':'Základný'}]}
            records=[category(page,'Top menu' if page==1 else 'A',None if page==1 else 1)] if path=='categories' else []
            return {path:records,'current_page':page,'current_page_items':len(records),
                    'number_of_items':2 if records else 0,'number_of_pages':2 if records else 0}
        with patch.dict(mapping._REGISTRY_PAGES,clear=True), patch.object(mapping.transport,'_request',side_effect=send):
            with self.assertRaises(mapping.transport.SourceError):
                mapping.read_registry(None,cache_key=fingerprint)
            result=mapping.read_registry(None,cache_key=fingerprint)
            self.assertEqual(len(result['category_registry']),2)
            for path in ('config','languages','pricelists','categories'):
                self.assertEqual(seen.count((path,1)),1)
            self.assertEqual(seen.count(('categories',2)),2)
            self.assertIsNotNone(mapping.recent_category_snapshot(fingerprint))
            mapping.invalidate_categories(fingerprint)
            self.assertIsNone(mapping.recent_category_snapshot(fingerprint))
            self.assertFalse(any(k[1]=='categories' for k in mapping._REGISTRY_PAGES))

    def test_pricing_failure_reports_only_the_failed_fixed_registry_name(self):
        for position, resource in enumerate(('config','languages','pricelists')):
            err=mapping.transport.SourceError('stock_publication_source_unavailable')
            err.upstream_status=503
            with self.subTest(resource=resource), patch.object(mapping.transport,'_request',
                    side_effect=[{}]*position+[err]), self.assertRaises(mapping.transport.SourceError) as observed:
                mapping.read_registry(None)
            self.assertEqual(observed.exception.catalog_resource,resource)
            self.assertEqual(observed.exception.upstream_status,503)
        with patch.object(mapping.transport,'_request',return_value={}), self.assertRaises(mapping.transport.SourceError) as invalid:
            mapping.read_registry(None)
        self.assertEqual(invalid.exception.catalog_resource,'pricing_options')

    def test_only_documented_query_arguments_on_native_registry(self):
        body={'current_page':1,'current_page_items':0,'number_of_pages':0,'number_of_items':0,'labels':[]}
        with patch.object(mapping.transport,'_request',return_value=body) as get:
            self.assertEqual(mapping.registry_pages(None,'labels'),[])
            get.assert_called_once_with(None,'get','labels',params={'page':1})
        paged=dict(body,availabilities=[])
        with patch.object(mapping.transport,'_request',return_value=paged) as get:
            mapping.registry_pages(None,'availabilities',kind='Custom')
            get.assert_called_once_with(None,'get','availabilities',params={'page':1,'type':'Custom'})

    def test_restricted_labels_never_guessed_but_unflagged_items_can_proceed(self):
        def read(client,path,key,filters,**kwargs):
            if path=='labels':
                exc=mapping.transport.SourceError('stock_publication_upgates_access');exc.upstream_status=403
                raise exc
            return []
        with (patch.object(mapping.publication,'_options',return_value={'prices_with_vat':True}),
              patch.object(mapping.batch,'collect_pages',side_effect=read)):
            result=mapping.read_registry(None)
        self.assertFalse(result['labels']);self.assertEqual(result['registry_gaps']['labels']['upstream_status'],403)

    def test_diagnostics_keep_quota_and_safe_resource_and_status(self):
        exc=mapping.transport.SourceError('stock_publication_rate_limited',429,retry_after=92,request_sent=False)
        exc.upstream_status=429
        with patch.object(mapping.batch,'collect_pages',side_effect=exc):
            with self.assertRaises(mapping.transport.SourceError) as observed:
                mapping._optional_registry(None,'availabilities',{},kind='InStock')
        self.assertEqual(observed.exception.catalog_resource,'availabilities_InStock')
        self.assertEqual(observed.exception.retry_after,92)
        with self.assertRaises(ValueError):mapping.registry_pages(None,'private/path')
