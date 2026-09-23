import copy
import asyncio
import json
import threading
import unittest
from contextlib import ExitStack
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import httpx
from fastapi import FastAPI
from catalog_fixtures import product
from inventory_hub.ai_content_types import Rule, RuleBook, Scope, JobAction, JobFork, UpdatePreviewRequest
from inventory_hub.services.ai_content_rules import resolve
from inventory_hub.services.catalog import CatalogError
from inventory_hub.services.catalog_merchandising import category_rows, category_chain, apply_availability, availability_policy
from inventory_hub.services.ai_content_update import patch_from_content, projection, values_match, mismatched_fields, confirm, identity
from inventory_hub.services import ai_content as service, ai_content_update as update
from inventory_hub.services.upgates import UpgatesError
from test_ai_content import content, context


class ProtectedRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from inventory_hub.routers import ai_content as routes
        self.routes=routes
        self.app=FastAPI()
        self.app.include_router(routes.router,prefix='/api')
        self.app.dependency_overrides[routes.access]=lambda: None
        async def session():
            yield AsyncMock()
        self.app.dependency_overrides[routes.get_session]=session
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app,raise_app_exceptions=False),base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_update_preview_returns_domain_error_status_and_details(self):
        error=CatalogError('ai_update_stock_unknown','Verify shop stock before changing availability',422)
        with patch.object(service,'get_job',AsyncMock(return_value=object())), patch.object(update,'prepare',AsyncMock(side_effect=error)):
            response=await self.client.post('/api/ai-content/jobs/test/update-preview',json={'expected_revision':7,'fields':['availability']})
        self.assertEqual(response.status_code,422)
        self.assertEqual(response.json(),{'detail':{'code':error.code,'message':str(error)}})

    async def test_missing_job_returns_404_instead_of_server_error(self):
        error=CatalogError('ai_job_not_found','AI job not found',404)
        with patch.object(service,'get_job',AsyncMock(side_effect=error)):
            response=await self.client.get('/api/ai-content/jobs/missing')
        self.assertEqual(response.status_code,404)
        self.assertEqual(response.json()['detail']['code'],'ai_job_not_found')

    async def test_error_translation_does_not_bypass_access_dependency(self):
        from fastapi import HTTPException
        def deny():
            raise HTTPException(401,detail={'code':'ai_access_required','message':'Unlock AI content'})
        self.app.dependency_overrides[self.routes.access]=deny
        with patch.object(service,'get_job',AsyncMock()) as get_job:
            response=await self.client.get('/api/ai-content/jobs/missing')
        self.assertEqual(response.status_code,401)
        get_job.assert_not_awaited()


class MerchandisingTests(unittest.TestCase):
    def test_ancestors_primary_and_cycle(self):
        rows = category_rows([{'code':'A','category_id':1}, {'code':'B','category_id':2,'parent_id':1}, {'code':'C','category_id':3,'parent_code':'B'}])
        self.assertEqual(category_chain(rows,'C'), [{'code':'A','main_yn':False},{'code':'B','main_yn':False},{'code':'C','main_yn':True}])
        for invalid in ([{'code':'A','parent_code':'X'}], [{'code':'A','parent_code':'B'},{'code':'B','parent_code':'A'}]):
            with self.assertRaises(CatalogError): category_chain(invalid,'A')

    def test_code_only_tree_does_not_make_root_its_own_parent(self):
        rows=category_rows([{'code':'A'}, {'code':'B','parent_code':'A'}])
        self.assertEqual(category_chain(rows,'B'),[{'code':'A','main_yn':False},{'code':'B','main_yn':True}])

    def test_unresolved_parent_id_is_not_silently_treated_as_root(self):
        with self.assertRaises(CatalogError):
            category_rows([{'code':'B','category_id':2,'parent_id':999}])

    def test_paul_lange_external_orderable_without_stock_write(self):
        p = product(supplier_stock=Decimal(0),supplier_external_available=True)
        payload = {'active_yn':True}
        apply_availability(payload,[p],availability_policy('paul-lange'))
        self.assertEqual(payload,{'active_yn':True,'availability':'do 5 dní','can_add_to_basket_yn':True})
        p.supplier_external_available = None
        apply_availability(payload,[p],availability_policy('paul-lange'))
        self.assertEqual(payload['availability'],'overíme')

    def test_northfinder_zero_remains_orderable_and_keeps_review_visibility(self):
        p = product(supplier_stock=Decimal(0))
        payload = {'active_yn':False,'variants':[{'code':p.shop_code,'active_yn':True}]}
        apply_availability(payload,[p],availability_policy('northfinder'))
        self.assertTrue(payload['variants'][0]['active_yn'])
        self.assertTrue(payload['variants'][0]['can_add_to_basket_yn'])
        self.assertEqual(payload['variants'][0]['availability'],'overíme')
        self.assertFalse(payload['active_yn'])
        p.supplier_stock = None
        payload = {'active_yn':True}
        apply_availability(payload,[p],availability_policy('northfinder'))
        self.assertTrue(payload['active_yn'])
        self.assertNotIn('stock',payload)

    def test_configured_supplier_labels_apply_to_exact_minimum_and_external_stock(self):
        policy = availability_policy('other-supplier', {'adapter_settings': {'availability': {'orderable': 'do 7 dní'}}})
        for changes in ({'supplier_stock': Decimal(2)}, {'supplier_stock_min': Decimal(6)},
                        {'supplier_external_available': True}):
            with self.subTest(changes=changes):
                payload = {'active_yn': False}
                apply_availability(payload, [product(**changes)], policy)
                self.assertEqual(payload['availability'], 'do 7 dní')
                self.assertFalse(payload['active_yn'])
                self.assertNotIn('stock', payload)
        payload = {'active_yn': True}
        apply_availability(payload, [product()], policy)
        self.assertEqual(payload['availability'], 'overíme')
        self.assertTrue(payload['can_add_to_basket_yn'])

    def test_import_rule_priority_and_conflict(self):
        book = RuleBook(rules=[Rule(id='a',name='a',import_policy={'supplier_name':'A'}),Rule(id='b',name='b',scope=Scope(supplier='test'),import_policy={'supplier_name':'B'})])
        self.assertEqual(resolve(book,Scope(supplier='test'))['import_policy']['supplier_name'],'B')
        book.rules.append(Rule(id='c',name='c',scope=Scope(supplier='test'),import_policy={'supplier_name':'C'}))
        with self.assertRaises(CatalogError): resolve(book,Scope(supplier='test'))

    def test_obsolete_availability_rule_conflicts_cannot_block_supplier_configuration(self):
        book = RuleBook(rules=[
            Rule(id='a',name='a',import_policy={'orderable':'A','unknown':'A','hide_zero_stock':True,'supplier_name':'Named supplier'}),
            Rule(id='b',name='b',import_policy={'orderable':'B','unknown':'B','hide_zero_stock':False}),
        ])
        self.assertEqual(resolve(book,Scope(supplier='test'))['import_policy'], {'supplier_name':'Named supplier'})


class UpdateTests(unittest.TestCase):
    def test_parameter_detail_endpoint_is_opt_in_and_uses_canonical_parameters(self):
        base={'code':'A/B','product_id':7,'stock':9,'prices':[{'price':20}]}
        parameter={'id':12,'descriptions':[{'language':'sk','name':'Hmotnosť'}],
                   'values':[{'id':13,'descriptions':[{'language':'sk','value':'150 g'}]}]}
        detail={'code':'A/B','product_id':7,'parameters_new':[parameter]}
        client=SimpleNamespace(get=Mock(side_effect=[{'products':[base]},{'products':[base]},{'products':[detail]}]))
        self.assertNotIn('parameters',update.read_product(client,'A/B'))
        result=update.read_product(client,'A/B',include_parameters=True)
        self.assertEqual(client.get.call_count,3)
        self.assertEqual(client.get.call_args.args[0],'products/A%2FB/parameters')
        self.assertEqual(result['parameters'],[{'descriptions':parameter['descriptions'],'values':[{'descriptions':parameter['values'][0]['descriptions']}]}])
        self.assertEqual(result['stock'],9)
        self.assertNotIn('id',result['parameters'][0])
        self.assertNotIn('parameters',base)

    def test_legacy_parameter_detail_and_variant_parameters_are_normalized(self):
        base={'code':'A','product_id':7,'variants':[{'code':'A-S','variant_id':8,'ean':'123'}]}
        detail={'code':'A','product_id':7,'parameters':[{'name':{'sk':'Farba'},'values':[{'sk':'Modrá'}]}],
                'variants':[{'code':'A-S','variant_id':8,'parameters_new':[{'descriptions':[{'language':'sk','name':'Veľkosť'}],
                    'values':[{'descriptions':[{'language':'sk','value':'S'}]}]}]}]}
        client=SimpleNamespace(get=Mock(side_effect=[{'products':[base]},{'products':[detail]}]))
        result=update.read_product(client,'A',include_parameters=True)
        self.assertEqual(result['parameters'][0]['descriptions'],[{'language':'sk','name':'Farba'}])
        self.assertEqual(result['variants'][0]['parameters'][0]['values'][0]['descriptions'],[{'language':'sk','value':'S'}])

    def test_parameter_readback_cannot_join_different_product_or_variant_identity(self):
        base={'code':'A','product_id':7,'variants':[{'code':'A-S','variant_id':8}]}
        for detail in ({'code':'A','product_id':99,'parameters_new':[]},
                       {'code':'OTHER','product_id':7,'parameters_new':[]},
                       {'code':'A','product_id':7,'parameters_new':[], 'variants':[]},
                       {'code':'A','product_id':7,'parameters_new':[], 'variants':[{'code':'A-S','variant_id':99,'parameters_new':[]}]}):
            with self.subTest(detail=detail):
                client=SimpleNamespace(get=Mock(side_effect=[{'products':[base]},{'products':[detail]}]))
                with self.assertRaises(CatalogError) as error:
                    update.read_product(client,'A',include_parameters=True)
                self.assertEqual(error.exception.code,'ai_update_identity')

    def test_missing_parameter_details_are_not_treated_as_empty_parameters(self):
        for detail in ({'code':'A','product_id':7}, {'code':'A','product_id':7,'parameters_new':[{'descriptions':[],'values':[None]}]}):
            with self.subTest(detail=detail):
                client=SimpleNamespace(get=Mock(side_effect=[{'products':[{'code':'A','product_id':7}]},{'products':[detail]}]))
                with self.assertRaises(CatalogError) as error:
                    update.read_product(client,'A',include_parameters=True)
                self.assertEqual(error.exception.code,'ai_update_parameters_unavailable')

    def test_only_explicit_system_category_codes_can_be_missing_after_import(self):
        payload={'code':'A','categories':[{'code':'SYSTEM','main_yn':False},{'code':'PARENT','main_yn':False},{'code':'LEAF','main_yn':True}]}
        preview={'payload':payload,'after':projection(payload,payload),'system_category_codes':['SYSTEM']}
        remote={'code':'A','categories':payload['categories'][1:]}
        self.assertTrue(values_match(remote,preview))
        remote['categories']=remote['categories'][1:]
        self.assertEqual(mismatched_fields(remote,preview),['categories'])

    def test_missing_system_category_requested_as_primary_is_still_a_mismatch(self):
        payload={'code':'A','categories':[{'code':'SYSTEM','main_yn':True}]}
        preview={'payload':payload,'after':projection(payload,payload),'system_category_codes':['SYSTEM']}
        self.assertEqual(mismatched_fields({'code':'A','categories':[]},preview),['categories'])

    def test_readback_diagnostics_identify_individual_text_fields_and_exclude_unselected_data(self):
        payload={'code':'A','descriptions':[{'language':'sk','short_description':'Short','long_description':'<p>Long</p>'}],
                 'parameters':[{'descriptions':[{'language':'sk','name':'Weight'}],'values':[{'descriptions':[{'language':'sk','value':'150'}]}]}]}
        preview={'payload':payload,'after':projection(payload,payload)}
        remote={'code':'A','descriptions':[{'language':'sk','short_description':'Short','long_description':'Other','seo_title':'Private untouched'}],
                'parameters':[],'stock':99,'prices':[{'price':50}],'metas':[{'key':'untouched','value':'not part of update'}]}
        self.assertEqual(mismatched_fields(remote,preview),['long_description','parameters'])
        observed=projection(remote,payload)
        self.assertEqual(set(observed),{'descriptions','parameters'})
        self.assertEqual(set(observed['descriptions'][0]),{'language','short_description','long_description'})

    def test_selected_fields_never_copy_prices_stock_identity_url_or_visibility(self):
        remote={'code':'A','stock':9,'active_yn':False,'ean':'123','prices':[{'price':19}], 'descriptions':[{'language':'sk','title':'Before','seo_url':'keep'}]}
        enriched={'code':'BAD','stock':100,'active_yn':True,'descriptions':[{'language':'sk','title':'After','seo_url':'bad','short_description':'Short'}]}
        payload=patch_from_content(remote,enriched,['title'],'sk')
        self.assertEqual(payload,{'code':'A','descriptions':[{'language':'sk','title':'After'}]})
        self.assertEqual(projection(remote,payload),{'descriptions':[{'language':'sk','title':'Before'}]})
        same=copy.deepcopy(remote);same['stock']=11
        self.assertEqual(projection(same,payload),projection(remote,payload))

    def test_parameter_updates_preserve_unrelated_existing_fields(self):
        def param(name,value):return {'descriptions':[{'language':'sk','name':name}],'values':[{'descriptions':[{'language':'sk','value':value}]}]}
        remote={'code':'A','parameters':[param('Existing','X'),param('Weight','1')]}
        enriched={'descriptions':[{'language':'sk'}],'parameters':[param('Weight','2')]}
        payload=patch_from_content(remote,enriched,['parameters'],'sk')
        self.assertEqual(payload['parameters'],[param('Existing','X'),param('Weight','2')])
        preview={'payload':payload,'after':projection(payload,payload)}
        self.assertTrue(values_match(payload,preview))
        payload['parameters'][1]['values'][0]['descriptions'][0]['value']='3'
        self.assertFalse(values_match(payload,preview))

    def test_localized_meta_update_preserves_other_languages_and_unrelated_metadata(self):
        remote = {'code':'A','metas':[
            {'key':'h1_descriptor','values':[{'language':'sk','value':'Stará pumpa'}, {'language':'cs','value':'Pumpička'}]},
            {'key':'validation_required','value':'1'}, {'key':'supplier_name','values':{'sk':'Dodávateľ'}}]}
        enriched = {'descriptions':[{'language':'sk'}], 'metas':[
            {'key':'h1_descriptor','values':[{'language':'sk','value':'Minipumpa'}]},
            {'key':'validation_required','value':'0'}, {'key':'supplier_name','value':'Wrong'}]}
        payload = patch_from_content(remote,enriched,['metas'],'sk')
        actual = {m['key']:m for m in payload['metas']}
        self.assertEqual(actual['h1_descriptor']['values'],[{'language':'cs','value':'Pumpička'},{'language':'sk','value':'Minipumpa'}])
        self.assertEqual(actual['validation_required']['value'],'1')
        self.assertEqual(actual['supplier_name'],remote['metas'][2])
        self.assertEqual(remote['metas'][0]['values'][0]['value'],'Stará pumpa')

    def test_meta_only_readback_checks_the_actual_language_and_preserved_translations(self):
        payload = {'code':'A','metas':[{'key':'h1_descriptor','values':[{'language':'cs','value':'Pumpička'}, {'language':'sk','value':'Pumpa'}]}]}
        preview = {'payload':payload,'after':projection(payload,payload),'language':'cs'}
        remote = copy.deepcopy(payload)
        remote['metas'][0]['values'][0]['value'] = 'Stará hodnota'
        self.assertFalse(values_match(remote,preview))
        remote['metas'][0]['values'][0]['value'] = 'Pumpička'
        self.assertTrue(values_match(remote,preview))
        remote['metas'][0]['values'].pop()
        self.assertFalse(values_match(remote,preview))

    def test_identity_survives_json_persistence_and_ignores_variant_order(self):
        remote = {'product_id':1,'code':'A','ean':None,'variants':[{'code':'B','variant_id':2,'ean':'123'},{'code':'C','variant_id':3,'ean':'456'}]}
        snapshot = json.loads(json.dumps(identity(remote)))
        remote['variants'].reverse()
        self.assertEqual(identity(remote),snapshot)
        remote['variants'][0]['ean']='changed'
        self.assertNotEqual(identity(remote),snapshot)

    def test_read_product_rejects_missing_duplicate_or_malformed_results(self):
        for products in (None,{},['bad'],[],[{'code':'A'},{'code':'A'}]):
            with self.subTest(products=products), patch.object(update.imports,'_get',return_value={'products':products}):
                with self.assertRaises(CatalogError):
                    update.read_product(object(),'A')


class UpdateExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.remote = {'product_id':7,'code':'A','ean':'123','stock':0,'descriptions':[{'language':'sk','title':'Before'}]}
        self.payload = {'code':'A','descriptions':[{'language':'sk','title':'After'}]}
        self.preview = {'id':'p','state':'ready','target':'target','fields':['title'],'payload':copy.deepcopy(self.payload),
            'supplier_availability': availability_policy('paul-lange'),
            'identity':identity(self.remote),'before':projection(self.remote,self.payload),'after':projection(self.payload,self.payload),'expires_at':'2999-01-01'}
        self.job = SimpleNamespace(revision=1,context={'shop':'test','supplier':'paul-lange','code':'A','update_preview':self.preview},status='review',events=[])
        self.client = SimpleNamespace(put=Mock(return_value={'products':[{'code':'A','updated_yn':True}]}))
        self.db = AsyncMock()
        self.db.scalar.return_value = 1
        self.request = SimpleNamespace(expected_revision=1,preview_id='p')
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(update.UpgatesClient,'from_shop',return_value=self.client))
        self.stack.enter_context(patch.object(update.imports,'shop_config',return_value={}))
        self.supplier_config = self.stack.enter_context(patch.object(update.imports,'supplier_config',return_value={}))
        self.stack.enter_context(patch.object(update.imports,'_target',return_value='target'))
        self.stack.enter_context(patch.object(service,'summary',return_value={}))

    async def run_confirm(self, reads):
        with patch.object(update,'read_product',side_effect=reads) as read:
            await confirm(self.db,self.job,self.request)
            return read

    async def test_success_records_intent_before_put_refreshes_cache_and_replay_is_read_only(self):
        after = {**self.remote,'descriptions':self.payload['descriptions']}
        def send(*args):
            self.assertEqual(self.db.commit.await_count,1)
            self.assertEqual(self.db.execute.await_count,1)
            self.assertIn('pg_advisory_xact_lock',str(self.db.execute.await_args.args[0]))
            self.assertEqual(self.job.context['update_preview']['state'],'sending')
            return {'products':[{'code':'A','updated_yn':True}]}
        self.client.put.side_effect=send
        await self.run_confirm([self.remote,self.remote,after])
        self.assertEqual(self.job.context['update_preview']['state'],'completed')
        self.db.refresh.assert_awaited_once_with(self.job,with_for_update=True)
        self.assertEqual(self.db.execute.await_count,2)
        cached = self.db.execute.await_args.args[0].compile().params
        self.assertEqual(cached['external_code'],'A')
        self.assertEqual(cached['data'],after)
        self.request.expected_revision = self.job.revision
        read = await self.run_confirm([])
        read.assert_not_called()
        self.client.put.assert_called_once()

    async def test_recreated_code_is_blocked_even_when_selected_text_is_unchanged(self):
        remote = {**self.remote,'product_id':99}
        with self.assertRaises(CatalogError) as error:
            await self.run_confirm([remote])
        self.assertEqual(error.exception.code,'ai_update_identity')
        self.client.put.assert_not_called()
        self.db.commit.assert_not_awaited()

    async def test_changed_stock_prevents_stale_availability_update(self):
        self.preview['fields'].append('availability')
        self.preview['shop_stock']=0
        with self.assertRaises(CatalogError) as error:
            await self.run_confirm([{**self.remote,'stock':2}])
        self.assertEqual(error.exception.code,'ai_shop_content_changed')
        self.client.put.assert_not_called()

    async def test_changed_supplier_config_blocks_ready_availability_but_not_uncertain_readback(self):
        self.preview['fields'].append('availability')
        self.preview['shop_stock'] = 0
        self.supplier_config.return_value = {'adapter_settings': {'availability': {'orderable': 'do 7 dní'}}}
        with self.assertRaises(CatalogError) as error:
            await self.run_confirm([self.remote])
        self.assertEqual(error.exception.code, 'supplier_availability_changed')
        self.client.put.assert_not_called()
        self.db.commit.assert_not_awaited()
        self.preview['state'] = 'uncertain'
        await self.run_confirm([self.remote])
        self.client.put.assert_not_called()
        self.assertEqual(self.job.context['update_preview']['state'], 'uncertain')

    async def test_legacy_availability_preview_requires_new_preview_before_put(self):
        self.preview['fields'].append('availability')
        self.preview['shop_stock'] = 0
        self.preview.pop('supplier_availability')
        with self.assertRaises(CatalogError) as error:
            await self.run_confirm([self.remote])
        self.assertEqual(error.exception.code, 'supplier_availability_changed')
        self.client.put.assert_not_called()

    async def test_unset_stock_becoming_positive_or_missing_prevents_stale_availability_update(self):
        self.preview['fields'].append('availability')
        self.preview['shop_stock']=None
        self.preview['availability_basis']='supplier_unset_shop_stock'
        for remote in ({**self.remote,'stock':2},{k:v for k,v in self.remote.items() if k!='stock'}):
            with self.subTest(stock_present='stock' in remote),self.assertRaises(CatalogError) as error:
                await self.run_confirm([remote])
            self.assertEqual(error.exception.code,'ai_shop_content_changed')
        self.client.put.assert_not_called()

    async def test_expired_or_wrong_target_preview_never_puts(self):
        for change, code in (({'expires_at':'2000-01-01'},'preview_expired'), ({'target':'other'},'shop_target_changed')):
            with self.subTest(change=change):
                self.preview.update(change)
                with self.assertRaises(CatalogError) as error:
                    await self.run_confirm([self.remote])
                self.assertEqual(error.exception.code,code)
        self.client.put.assert_not_called()

    async def test_timeout_after_accepted_put_is_successful_after_readback(self):
        self.client.put.side_effect=UpgatesError('do not expose upstream detail')
        after = {**self.remote,'descriptions':self.payload['descriptions']}
        await self.run_confirm([self.remote,self.remote,after])
        self.assertEqual(self.job.context['update_result'],{'status':'completed','fields':['title']})
        self.client.put.assert_called_once()

    async def test_readback_failure_preserves_durable_uncertainty_and_reconcile_never_puts(self):
        await self.run_confirm([self.remote,self.remote,CatalogError('upgates_read_failed','private body',502)])
        self.assertEqual(self.job.context['update_preview']['state'],'uncertain')
        self.assertEqual(self.job.context['update_result']['error'],'upgates_read_failed')
        self.request.expected_revision=self.job.revision
        await self.run_confirm([self.remote])
        self.client.put.assert_called_once()
        self.assertEqual(self.job.context['update_preview']['state'],'uncertain')
        self.assertEqual(self.db.execute.await_count,1)

    async def test_explicit_rejection_is_actionable_without_repeated_put(self):
        self.client.put.side_effect=UpgatesError('secret upstream text',status_code=422)
        await self.run_confirm([self.remote,self.remote,self.remote])
        self.assertEqual(self.job.context['update_result'],{'status':'rejected','fields':['title'],'error':'upgates_update_http_422'})
        self.assertNotIn('secret',str(self.job.context))
        self.request.expected_revision=self.job.revision
        with self.assertRaises(CatalogError):
            await self.run_confirm([])
        self.client.put.assert_called_once()

    async def test_rejected_authentication_does_not_repeat_bad_login(self):
        self.client.put.side_effect=UpgatesError('secret',status_code=401)
        read = await self.run_confirm([self.remote,self.remote])
        self.assertEqual(read.call_count,2)
        self.assertEqual(self.job.context['update_preview']['state'],'rejected')

    async def test_http_200_row_rejection_is_not_permanently_uncertain(self):
        self.client.put.return_value={'products':[{'code':'A','updated_yn':False}]}
        await self.run_confirm([self.remote,self.remote,self.remote])
        self.assertEqual(self.job.context['update_result']['status'],'rejected')

    async def test_readback_cannot_confirm_different_product_with_same_text(self):
        after = {**self.remote,'product_id':99,'descriptions':self.payload['descriptions']}
        await self.run_confirm([self.remote,self.remote,after])
        self.assertEqual(self.job.context['update_preview']['state'],'uncertain')
        self.assertEqual(self.db.execute.await_count,1)

    async def test_uncertain_readback_returns_selected_mismatch_and_observed_values_without_put(self):
        self.preview['state']='uncertain'
        remote={**self.remote,'prices':[{'price':99}],'metas':[{'key':'private','value':'not selected'}]}
        await self.run_confirm([remote])
        result=self.job.context['update_result']
        self.assertEqual(result['error'],'ai_update_readback_mismatch')
        self.assertEqual(result['mismatched_fields'],['title'])
        self.assertEqual(result['observed'],{'descriptions':[{'language':'sk','title':'Before'}]})
        self.client.put.assert_not_called()

    async def test_historical_uncertain_preview_reconciles_parameters_and_documented_system_root_without_put(self):
        parameters=[{'descriptions':[{'language':'sk','name':'Hmotnosť'}],'values':[{'descriptions':[{'language':'sk','value':'150'}]}]}]
        self.preview.update(state='uncertain',fields=['parameters','categories'])
        self.preview['payload']={'code':'A','parameters':parameters,'categories':[{'code':'SYSTEM','main_yn':False},{'code':'LEAF','main_yn':True}]}
        self.preview['after']=projection(self.preview['payload'],self.preview['payload'])
        remote={**self.remote,'parameters':parameters,'categories':[{'code':'LEAF','main_yn':True}]}
        with patch.object(update.imports,'cached_import_options',return_value={'categories':[{'code':'SYSTEM','system_root':True}]}):
            read=await self.run_confirm([remote])
        self.assertEqual(self.job.context['update_preview']['state'],'completed')
        self.assertEqual(self.job.context['update_preview']['system_category_codes'],['SYSTEM'])
        read.assert_called_once_with(self.client,'A',include_parameters=True)
        self.client.put.assert_not_called()

    async def test_readback_preserves_concurrent_archive_change(self):
        async def refresh(*args,**kwargs):
            self.job.context={**self.job.context,'archived':True}
        self.db.refresh.side_effect=refresh
        await self.run_confirm([self.remote,self.remote,{**self.remote,'descriptions':self.payload['descriptions']}])
        self.assertTrue(self.job.context['archived'])

    async def test_locked_preflight_rejects_changed_identity_content_or_stock_without_put(self):
        self.preview['fields'].append('availability')
        self.preview['shop_stock']=0
        for changes,code in (({'product_id':99},'ai_update_identity'),
                            ({'descriptions':[{'language':'sk','title':'Other update'}]},'ai_shop_content_changed'),
                            ({'stock':2},'ai_shop_content_changed')):
            with self.subTest(changes=changes):
                self.job.revision=1
                self.preview['state']='ready'
                self.job.context['update_preview']=self.preview
                await self.run_confirm([self.remote,{**self.remote,**changes}])
                self.assertEqual(self.job.context['update_result']['status'],'rejected')
                self.assertEqual(self.job.context['update_result']['error'],code)
        self.client.put.assert_not_called()

    async def test_failed_locked_preflight_remains_safe_to_prepare_again(self):
        await self.run_confirm([self.remote,CatalogError('upgates_read_failed','private',502)])
        self.assertEqual(self.job.context['update_result'],{'status':'rejected','fields':['title'],'error':'upgates_read_failed'})
        self.client.put.assert_not_called()

    async def test_two_jobs_for_same_product_serialize_and_only_first_updates(self):
        # Both optimistic reads see the old value. The transaction-scoped lock
        # must then make the losing job see the winner's change before its PUT.
        lock=asyncio.Lock()
        guard=threading.Lock()
        reads_started=threading.Barrier(2)
        remote=copy.deepcopy(self.remote)
        read_count=0
        writes=[]
        lock_keys=[]
        jobs=[]
        def read(*args,**kwargs):
            nonlocal read_count
            with guard:
                index=read_count
                read_count+=1
                snapshot=copy.deepcopy(remote)
            if index < 2:
                reads_started.wait(timeout=3)
            return snapshot
        def put(path,body):
            with guard:
                writes.append(copy.deepcopy(body))
                remote.update(copy.deepcopy(body['products'][0]))
            return {'products':[{'code':'A','updated_yn':True}]}
        async def run(name):
            db=AsyncMock()
            db.scalar.return_value=1
            holds_lock=False
            async def execute(statement,params=None):
                nonlocal holds_lock
                if 'pg_advisory_xact_lock' in str(statement):
                    lock_keys.append(params['key'])
                    await lock.acquire()
                    holds_lock=True
            async def commit():
                nonlocal holds_lock
                if holds_lock:
                    holds_lock=False
                    lock.release()
            db.execute.side_effect=execute
            db.commit.side_effect=commit
            preview=copy.deepcopy(self.preview)
            preview['id']=name
            preview['payload']['descriptions'][0]['title']=name
            preview['after']=projection(preview['payload'],preview['payload'])
            job=SimpleNamespace(revision=1,context={'shop':'test','code':'A','update_preview':preview},status='review',events=[])
            jobs.append(job)
            try:
                await confirm(db,job,SimpleNamespace(expected_revision=1,preview_id=name))
            finally:
                await db.commit()
        self.client.put.side_effect=put
        with patch.object(update,'read_product',side_effect=read):
            await asyncio.wait_for(asyncio.gather(run('first'),run('second')),timeout=5)
        self.assertEqual(len(writes),1)
        self.assertEqual(len(lock_keys),2)
        self.assertEqual(lock_keys[0],lock_keys[1])
        self.assertEqual(sorted(j.context['update_result']['status'] for j in jobs),['completed','rejected'])
        losing=next(j for j in jobs if j.context['update_result']['status']=='rejected')
        self.assertEqual(losing.context['update_result']['error'],'ai_shop_content_changed')

    async def test_uncertain_update_only_reads_and_stale_update_never_writes(self):
        from inventory_hub.services import ai_content_update as u
        remote={'code':'A','descriptions':[{'language':'sk','title':'Before'}]}
        payload={'code':'A','descriptions':[{'language':'sk','title':'After'}]}
        preview={'id':'p','state':'uncertain','target':'target','fields':['title'],'payload':payload,'before':projection(remote,payload),'after':projection(payload,payload),'expires_at':'2999-01-01'}
        job=SimpleNamespace(revision=1,context={'shop':'test','code':'A','update_preview':preview},status='review',events=[])
        client=SimpleNamespace(put=AsyncMock())
        request=SimpleNamespace(expected_revision=1,preview_id='p')
        with patch.object(u.UpgatesClient,'from_shop',return_value=client), patch.object(u.imports,'shop_config',return_value={}), patch.object(u.imports,'_target',return_value='target'), patch.object(u,'read_product',return_value=remote), patch.object(u.service,'summary',return_value={}):
            await confirm(AsyncMock(),job,request)
            client.put.assert_not_called()
            self.assertEqual(job.context['update_preview']['state'],'uncertain')
            preview['state']='ready';preview['before']={}
            job.context['update_preview']=preview;job.revision=1
            with self.assertRaises(CatalogError):await confirm(AsyncMock(),job,request)
            client.put.assert_not_called()


class UpdatePreparationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source=product(supplier_stock=Decimal(6))
        ctx=context([self.source],supplier='paul-lange',feed_key='products',product_ids=[1],
            code=self.source.shop_code,name=self.source.name,target='target')
        ctx['source_digest']=service.source_digest([self.source])
        self.job=SimpleNamespace(kind='product',status='review',revision=1,context=ctx,output=content().model_dump(),usage={},events=[])
        self.remote={'product_id':7,'code':self.source.shop_code,'ean':self.source.eans[0], 'stock':0,
            'availability':'Overíme','active_yn':True,'descriptions':[{'language':'sk','title':'Before'}]}
        self.client=SimpleNamespace(put=Mock())
        self.stack=ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(update.UpgatesClient,'from_shop',return_value=self.client))
        self.stack.enter_context(patch.object(update.imports,'shop_config',return_value={}))
        self.supplier_config = self.stack.enter_context(patch.object(update.imports,'supplier_config',return_value={}))
        self.stack.enter_context(patch.object(update.imports,'_target',return_value='target'))
        self.stack.enter_context(patch.object(update,'selected_products',AsyncMock(return_value=[self.source])))
        self.stack.enter_context(patch.object(update,'read_product',side_effect=lambda *a,**kw:copy.deepcopy(self.remote)))
        self.stack.enter_context(patch.object(service,'summary',return_value={}))

    async def prepare(self,fields):
        await update.prepare(AsyncMock(),self.job,UpdatePreviewRequest(expected_revision=self.job.revision,fields=fields))
        return self.job.context['update_preview']

    async def test_text_only_preview_skips_metadata_api_and_never_writes(self):
        with patch.object(update,'content_fields') as metadata:
            preview=await self.prepare(['title'])
        metadata.assert_not_called()
        self.client.put.assert_not_called()
        self.assertEqual(set(preview['payload']),{'code','descriptions'})
        self.assertEqual(preview['identity'],identity(self.remote))

    async def test_existing_local_stock_keeps_current_availability_and_visibility(self):
        self.remote.update(stock=3,availability='SKLADOM',active_yn=False)
        preview=await self.prepare(['availability'])
        self.assertEqual(preview['payload'],{'code':self.source.shop_code,'availability':'SKLADOM'})
        self.assertEqual(preview['shop_stock'],3)
        self.assertNotIn('active_yn',preview['payload'])

    async def test_zero_local_stock_uses_supplier_lead_time_without_stock_write(self):
        preview=await self.prepare(['availability'])
        self.assertEqual(preview['payload'],{'code':self.source.shop_code,'availability':'do 5 dní'})

    async def test_supplier_config_overrides_frozen_ai_labels_without_changing_cart_or_visibility(self):
        self.supplier_config.return_value = {'adapter_settings': {'availability': {'orderable': 'do 7 dní'}}}
        self.job.context['resolved']['import_policy'] = {'orderable': 'AI value', 'unknown': 'AI unknown', 'hide_zero_stock': True}
        preview = await self.prepare(['availability'])
        self.assertEqual(preview['payload'], {'code': self.source.shop_code, 'availability': 'do 7 dní'})
        self.assertEqual(preview['supplier_availability']['orderable'], 'do 7 dní')
        self.source.supplier_stock = Decimal(0)
        preview = await self.prepare(['availability'])
        self.assertEqual(preview['payload'], {'code': self.source.shop_code, 'availability': 'overíme'})

    async def test_explicit_unset_stock_uses_supplier_availability_without_inventing_quantity(self):
        self.remote.update(stock=None,stocks=[{'quantity':None},{'quantity':None}],active_yn=False)
        self.source.supplier_stock=Decimal(0)
        self.source.supplier_external_available=True
        preview=await self.prepare(['availability'])
        self.assertEqual(preview['availability_basis'],'supplier_unset_shop_stock')
        self.assertIsNone(preview['shop_stock'])
        self.assertEqual(preview['payload'],{'code':self.source.shop_code,'availability':'do 5 dní'})
        self.assertIsNone(self.remote['stock'])
        self.assertFalse(self.remote['active_yn'])

    async def test_missing_stock_field_or_malformed_value_is_not_assumed_unset(self):
        self.remote.pop('stock')
        with self.assertRaises(CatalogError) as error:
            await self.prepare(['availability'])
        self.assertEqual(error.exception.code,'ai_update_stock_unknown')
        for stock in ('unknown','null','NaN','Infinity',True):
            self.remote['stock']=stock
            with self.subTest(stock=stock), self.assertRaises(CatalogError) as error:
                await self.prepare(['availability'])
            self.assertEqual(error.exception.code,'ai_update_stock_unknown')
        self.client.put.assert_not_called()

    async def test_source_changed_or_target_changed_blocks_preparation(self):
        self.source.name='Supplier changed name'
        with self.assertRaises(CatalogError) as error:
            await self.prepare(['title'])
        self.assertEqual(error.exception.code,'ai_source_changed')
        self.job.context['target']='different'
        with self.assertRaises(CatalogError) as error:
            await self.prepare(['title'])
        self.assertEqual(error.exception.code,'shop_target_changed')

    async def test_missing_parameter_registry_is_explained_and_sends_nothing(self):
        with self.assertRaises(CatalogError) as error:
            await self.prepare(['parameters'])
        self.assertEqual(error.exception.code,'ai_parameter_registry_missing')
        self.client.put.assert_not_called()

    async def test_family_must_match_all_remote_variant_identities(self):
        self.remote['variants']=[{'code':self.source.shop_code,'ean':self.source.eans[0]}, {'code':'OTHER','ean':'other'}]
        with self.assertRaises(CatalogError) as error:
            await self.prepare(['title'])
        self.assertEqual(error.exception.code,'ai_update_family')

    async def test_category_update_preserves_secondary_branch_with_all_ancestors(self):
        self.job.context['options']['category_code']='NEW'
        self.remote['categories']=[{'category_id':3,'main_yn':True}]
        categories=[{'code':'ROOT','category_id':1}, {'code':'OLD','category_id':2,'parent_code':'ROOT'},
                    {'code':'LEAF','category_id':3,'parent_code':'OLD'}, {'code':'NEW','category_id':4,'parent_code':'ROOT'}]
        with patch.object(update.imports,'cached_import_options',return_value={'categories':categories}):
            preview=await self.prepare(['categories'])
        self.assertEqual(preview['payload']['categories'],[
            {'code':'ROOT','main_yn':False},{'code':'OLD','main_yn':False},
            {'code':'LEAF','main_yn':False},{'code':'NEW','main_yn':True}])
        by_code={c['code']:c['category_id'] for c in categories}
        remote={'code':self.remote['code'],'categories':[{'category_id':by_code[c['code']],'main_yn':c['main_yn']}
            for c in preview['payload']['categories']]}
        self.assertTrue(values_match(remote,preview))

    async def test_shop_only_preparation_uses_remote_source_without_catalog_ids(self):
        from inventory_hub.services import ai_content_existing as existing
        self.remote['manufacturer']='TEST'
        ctx=self.job.context
        ctx.update(source_kind='shop',update_only=True,product_ids=[],source_parameters_loaded=True)
        ctx['source_digest']=service.digest(existing.source_snapshot(self.remote))
        with patch.object(update,'selected_products',AsyncMock()) as selected:
            preview=await self.prepare(['title'])
            selected.assert_not_awaited()
        self.assertEqual(preview['payload']['code'],self.remote['code'])
        with self.assertRaises(CatalogError) as error:
            await self.prepare(['availability'])
        self.assertEqual(error.exception.code,'ai_update_source_availability')


class JobLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def job(self, **values):
        return SimpleNamespace(id='source',kind='product',status='failed',revision=1,events=[],
            context={'product_ids':[1,2]},output=None,preview_id=None,error='original',**values)

    async def test_archive_restore_preserve_content_costs_and_block_inflight_operations(self):
        job=self.job()
        job.output=content().model_dump()
        job.actual_usd=Decimal('0.15')
        original=copy.deepcopy(job.output)
        with patch.object(service,'summary',return_value={}):
            await service.action(AsyncMock(),job,JobAction(expected_revision=1,action='archive'))
            self.assertTrue(job.context['archived'])
            await service.action(AsyncMock(),job,JobAction(expected_revision=job.revision,action='restore'))
            self.assertFalse(job.context['archived'])
            self.assertEqual(job.output,original)
            self.assertEqual(job.actual_usd,Decimal('0.15'))
            for status in ('queued','generating','preparing_import','import_queued','importing'):
                job.status=status
                with self.subTest(status=status), self.assertRaises(CatalogError):
                    await service.action(AsyncMock(),job,JobAction(expected_revision=job.revision,action='archive'))

    async def test_pending_updates_block_mutations_and_forks(self):
        for state in ('sending','uncertain'):
            for action in ('archive','restore','cancel','reopen','start','import','retry_import'):
                job=self.job()
                job.context['update_preview']={'state':state}
                with self.subTest(state=state,action=action), self.assertRaises(CatalogError) as error:
                    await service.action(AsyncMock(),job,JobAction(expected_revision=1,action=action))
                self.assertEqual(error.exception.code,'ai_update_uncertain')
            job=self.job()
            job.context['update_preview']={'state':state}
            with self.assertRaises(CatalogError):
                await service.fork_job(AsyncMock(),job,JobFork(expected_revision=1,product_ids=[1]))

    async def test_unknown_import_cannot_reopen_or_fork_into_duplicate_creation(self):
        job=self.job()
        job.status='import_failed';job.output=content().model_dump();job.preview_id='old'
        with patch.object(service,'summary',return_value={'import_result':{'items':[{'status':'uncertain'}]}}), patch.object(service,'create_batch',AsyncMock()) as create:
            with self.assertRaises(CatalogError):
                await service.fork_job(AsyncMock(),job,JobFork(expected_revision=1,product_ids=[1]))
            with self.assertRaises(CatalogError):
                await service.action(AsyncMock(),job,JobAction(expected_revision=1,action='reopen'))
            create.assert_not_awaited()

    async def test_fork_reuses_only_selected_evidence_and_parameters_and_invalidates_replay(self):
        job=self.job()
        job.status='blocked';job.usage={'opened_sources':['https://example.test/product']}
        job.context.update(supplier='paul-lange',feed_key='products',category_profile='general',shop='test',
            options={'language':'sk'},resolved={'policy':{}},research='feed_only',sale_price_overrides={'1':'12','2':'13'})
        job.output=content(parameters=[{'name':'Size','values':['S'],'product_id':1},{'name':'Size','values':['M'],'product_id':2}],
            evidence=[{'claim':'one','source':'feed:1','quote':'one'},{'claim':'two','source':'feed:2','quote':'two'}]).model_dump()
        fresh=SimpleNamespace(id='fresh',context={'product_ids':[1]},status='estimate',revision=1,events=[],output=None,usage=None)
        request=JobFork(expected_revision=1,product_ids=[1])
        with patch.object(service,'summary',return_value={}), patch.object(service,'create_batch',AsyncMock(return_value=[{'id':'fresh'}])) as create, patch.object(service,'get_job',AsyncMock(return_value=fresh)), patch.object(service,'validate_content',return_value={'errors':[]}):
            await service.fork_job(AsyncMock(),job,request)
            self.assertEqual(fresh.status,'review')
            self.assertEqual(fresh.actual_usd,Decimal(0))
            self.assertEqual([p['product_id'] for p in fresh.output['parameters']],[1])
            self.assertEqual([e['source'] for e in fresh.output['evidence']],['feed:1'])
            batch=create.await_args.args[1]
            self.assertEqual(batch.sale_price_overrides,{1:'12'})
            self.assertTrue(batch.targets[0].policy.review_required)
            with self.assertRaises(CatalogError):
                await service.fork_job(AsyncMock(),job,request)
            self.assertEqual(create.await_count,1)
