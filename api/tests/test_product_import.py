"""Import snapshots preserve money, manual work and the existing write boundaries."""
import asyncio
import unittest
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from pydantic import ValidationError
from catalog_fixtures import product
from test_ai_content import content
from inventory_hub.catalog_types import ShopImportOptions
from inventory_hub.product_import_types import ImportValues, DraftPatch
from inventory_hub.services import product_import as service, ai_content, ai_content_update, catalog_import
from inventory_hub.services.catalog import CatalogError


def row(id=1, **values):
    p = product(id)
    defaults = dict(code=p.shop_code, supplier_code=p.code, name=p.name, brand=p.brand, eans=p.eans,
        images=p.images, description_html='<p>Popis</p>', sale_gross='123.00', retail_gross='123.00', purchase_net='60.00',
        vat_percent='23.00', currency='EUR', category_code='LEAF', availability='do 5 dní')
    defaults.update(values)
    data = ImportValues(**defaults).model_dump(mode='json')
    return {'id': id, 'group_key': p.shop_code, 'is_variant': False, 'source': p.model_dump(mode='json'),
        'values': data, 'manual_fields': [], 'provenance': {}, 'ai_baseline': deepcopy(data)}


def document(rows=None):
    return {'rows': rows or [row()], 'categories': [
        {'code': 'MENU', 'system_root': True}, {'code': 'ROOT', 'parent_code': 'MENU'},
        {'code': 'LEAF', 'parent_code': 'ROOT'}], 'options': ShopImportOptions().model_dump()}


class ProductImportUnitTests(unittest.TestCase):
    def test_receiving_publication_rechecks_saved_identity_before_remote_write(self):
        staged = document()
        staged['receiving_origin'] = {'kind': 'receiving', 'session_id': 17}
        staged['rows'][0]['hub_product_id'] = 41
        draft = SimpleNamespace(status='saved', document=staged)
        snapshot = {'draft_id': 'fixture', 'hash': service.content_hash(staged)}
        with patch.object(service, 'get_draft', AsyncMock(return_value=draft)):
            for identities in ({}, {1: 42}):
                with patch.object(catalog_import, 'local_identities', AsyncMock(return_value=(identities, set()))):
                    with self.assertRaises(CatalogError) as changed:
                        asyncio.run(service.assert_snapshot(AsyncMock(), snapshot))
                    self.assertEqual(changed.exception.code, 'local_identity_conflict')
            with patch.object(catalog_import, 'local_identities', AsyncMock(return_value=({1: 41}, set()))):
                self.assertIs(asyncio.run(service.assert_snapshot(AsyncMock(), snapshot)), draft)

    def test_new_preview_requires_proved_zero_product_attempts_across_all_targets(self):
        draft = SimpleNamespace(shop='biketrek', document={'publications': {
            'biketrek': {'preview_id': 'first'}, 'xtrek': {'preview_id': 'second'}}})
        expired = {'status': 'failed', 'errors': ['import_source_availability_expired'],
            'items': [{'status': 'ready', 'errors': [], 'product_write_attempted': False}]}
        with patch.object(service, 'target_result', return_value=expired):
            self.assertTrue(service.source_preview_retryable(draft))
        for state, attempted in [('created', True), ('uncertain', True), ('failed', True), ('ready', None)]:
            blocked = deepcopy(expired)
            blocked['items'][0].update(status=state, product_write_attempted=attempted)
            with patch.object(service, 'target_result', side_effect=lambda shop, _: expired if shop == 'biketrek' else blocked):
                self.assertFalse(service.source_preview_retryable(draft))

    def test_multiple_shop_outcomes_keep_unstarted_and_uncertain_targets_visible(self):
        previews = {shop: {'preview_id': shop, 'items': [{'product_ids': [1], 'status': 'ready'}]}
                    for shop in ('biketrek', 'xtrek')}
        draft = SimpleNamespace(shop='biketrek', document={'publication': {'preview_id': 'aggregate'}, 'publications': previews})
        delivered = {'status': 'completed', 'items': [{'product_ids': [1], 'status': 'created',
            'shop_url': 'https://shop.example/item'}]}
        with patch.object(service, 'target_result', side_effect=lambda shop, _: delivered if shop == 'biketrek' else None):
            result = service.publication_result(draft)
            self.assertEqual(service.publication_state(result), 'partial')
            self.assertEqual([(item['shop'], item['status']) for item in result['items']],
                             [('biketrek', 'created'), ('xtrek', 'pending')])
            with self.assertRaises(CatalogError) as error:
                service.assert_editable(draft)
            self.assertEqual(error.exception.code, 'import_publication_pending')
        uncertain = {'status': 'completed', 'items': [{'product_ids': [1], 'status': 'uncertain'}]}
        with patch.object(service, 'target_result', side_effect=lambda shop, _: delivered if shop == 'biketrek' else uncertain):
            self.assertEqual(service.publication_state(service.publication_result(draft)), 'uncertain')

    def test_all_shop_previews_are_checked_before_any_target_is_queued(self):
        previews = {shop: {'preview_id': shop, 'items': [{'product_ids': [1], 'status': 'ready'}]}
                    for shop in ('biketrek', 'xtrek')}
        draft = SimpleNamespace(id='draft', document={'publications': previews})
        previews['xtrek']['items'][0]['status'] = 'invalid'
        with patch.object(catalog_import, 'queue_import') as queue, self.assertRaises(CatalogError):
            service.queue_publications(draft)
        queue.assert_not_called()
        previews['xtrek']['items'][0]['status'] = 'ready'
        with patch.object(catalog_import, 'queue_import', side_effect=[({}, False), ({}, True)]) as queue:
            self.assertEqual(service.queue_publications(draft, True), [('xtrek', 'xtrek')])
        self.assertEqual([call.args[:2] for call in queue.call_args_list], [('biketrek', 'biketrek'), ('xtrek', 'xtrek')])
        self.assertTrue(all(call.kwargs['staging_id'] == 'draft' for call in queue.call_args_list))

    def test_target_selection_distinguishes_hub_only_from_legacy_single_shop(self):
        self.assertEqual(service.row_targets(row(), 'biketrek'), ['biketrek'])
        self.assertEqual(service.row_targets(row(target_shops=[]), 'biketrek'), [])
        self.assertEqual(service.row_targets(row(target_shops=['xtrek', 'biketrek']), 'biketrek'), ['xtrek', 'biketrek'])
        for value in (['other'], ['biketrek', 'biketrek']):
            with self.assertRaises(ValidationError):
                row(target_shops=value)

    def test_delivery_status_is_separate_from_ai_readiness_and_scoped_to_products(self):
        first, second = row(), row(2)
        first['ai_job_id'], second['ai_job_id'] = 'ai-1', 'ai-2'
        draft = SimpleNamespace(id='draft', revision=9, document=document([first, second]))
        job = SimpleNamespace(id='ai-1', status='ready')
        result = {'status': 'completed', 'items': [
            {'product_ids': [1], 'status': 'created'}, {'product_ids': [2], 'status': 'exists'}]}
        state = service.ai_job_import_status(job, draft, result)
        self.assertEqual(state['publication_status'], 'completed')
        self.assertTrue(state['publication_finished'])
        self.assertFalse(state['ai_applied'], 'Successful feed publication must not claim AI was used')
        self.assertEqual(job.status, 'ready', 'Reading delivery does not rewrite AI history')
        result['status'], result['items'][1]['status'] = 'failed', 'failed'
        self.assertEqual(service.publication_state(result), 'partial')
        self.assertEqual(service.ai_job_import_status(job, draft, result)['publication_status'], 'completed')
        job.id = 'ai-2'
        self.assertEqual(service.ai_job_import_status(job, draft, result)['publication_status'], 'failed')
        result['items'][1]['status'] = 'uncertain'
        self.assertEqual(service.publication_state(result), 'uncertain')
        job.id = 'replaced'
        state = service.ai_job_import_status(job, draft, result)
        self.assertFalse(state['linked'])
        self.assertIsNone(state['publication_status'])
        self.assertIsNone(service.publication_state(None))

    def test_prices_use_net_purchase_and_gross_sale_without_stock(self):
        item = row(purchase_net='12.99', vat_percent='23.00', sale_gross='30.00')
        source = service.to_source(item)
        self.assertEqual(source.prices.purchase_gross, Decimal('15.9777'))
        payload = catalog_import.build_item([source], ShopImportOptions(), {}, False, {1: Decimal('30')})
        service.overlay_payload(payload, [item], 'sk', document()['categories'])
        self.assertEqual(payload.payload['prices'][0]['price_purchase'], 12.99)
        self.assertEqual(payload.payload['prices'][0]['pricelists'][0]['price_original'], 24.39)
        self.assertEqual(payload.payload['categories'], [{'code':'ROOT','main_yn':False}, {'code':'LEAF','main_yn':True}])
        catalog_import._assert_payload(payload.payload)
        self.assertFalse(payload.payload['active_yn'])

    def test_ai_preserves_manual_cells_and_existing_category(self):
        item = row()
        item['values']['name'] = 'Ručný názov'
        item['values']['seo_title'] = 'Ručné SEO'
        item['manual_fields'] = ['seo_title']
        out = content()
        service.merge_ai_values(item, out, {'options': {'category_code': 'DIFFERENT'}})
        self.assertEqual(item['values']['name'], 'Ručný názov')
        self.assertEqual(item['values']['seo_title'], 'Ručné SEO')
        self.assertEqual(item['values']['category_code'], 'LEAF')
        self.assertEqual(item['values']['description_html'], out.long_description)
        self.assertEqual(item['provenance']['description_html'], 'ai')

    def test_group_parent_parameters_exclude_variant_axes_and_nonshared_values(self):
        rows = [row(), row(2)]
        for index, item in enumerate(rows):
            item['is_variant'] = True
            item['group_key'] = 'PL-G-X'
            item['source'].update(group_code='X', variant_relationship='explicit')
            item['values']['variant_attributes'] = [{'name':'Farba', 'value':['red','blue'][index]}]
            item['values']['parameters'] = [{'name':'Farba','value':['red','blue'][index]}, {'name':'Materiál','value':'Guma'}]
        built = catalog_import.build_item([service.to_source(r) for r in rows], ShopImportOptions(), {}, True,
            {1:Decimal('123'),2:Decimal('123')})
        service.overlay_payload(built, rows, 'sk', document()['categories'])
        self.assertEqual([p['descriptions'][0]['name'] for p in built.payload['parameters']], ['Materiál'])
        self.assertEqual([v['parameters'][0]['values'][0]['descriptions'][0]['value'] for v in built.payload['variants']], ['blue','red'])

    def test_no_registry_keeps_feed_parameters_and_active_html_stays_blocked(self):
        item = row(parameters=[{'name':'Farba', 'value':'modrá'}])
        service.merge_ai_values(item, content(), {'resolved':{'category':{}}})
        self.assertEqual(item['values']['parameters'], [{'name':'Farba', 'value':'modrá'}])
        item['values']['description_html'] = '<p class="fine">Valid <span style="color:red">HTML</span></p>'
        service.validate_rows(document([item]))
        self.assertNotIn('ai_unsafe_html', item['errors'])
        item['values']['description_html'] = '<img src=x onerror="alert(1)">'
        service.validate_rows(document([item]))
        self.assertIn('ai_unsafe_html', item['errors'])

    def test_seo_url_is_writable_key_and_manufacturer_code_stays_source_only(self):
        item = row(seo_url='custom-product', manufacturer_code='M-123')
        built = catalog_import.build_item([service.to_source(item)], ShopImportOptions(), {}, True)
        service.overlay_payload(built, [item], 'sk', document()['categories'])
        self.assertEqual(built.payload['descriptions'][0]['seo_url'], 'custom-product')
        self.assertNotIn('url', built.payload['descriptions'][0])
        self.assertNotIn('code_manufacturer', built.payload)

    def test_ai_identity_changed_does_not_apply_stale_product(self):
        item = row()
        for field, value in [('manufacturer_code', 'Different model'), ('category_code', 'Different category'), ('variant_attributes', [{'name':'Veľkosť','value':'XL'}])]:
            with self.subTest(field=field):
                item = row()
                item['values'][field] = value
                with self.assertRaises(CatalogError) as raised:
                    service.merge_ai_values(item, content(), {})
                self.assertEqual(raised.exception.code, 'import_ai_identity_changed')

    def test_ai_only_fills_unmapped_category_and_preserves_safety(self):
        item = row(category_code=None)
        item['source']['safety_information'] = '<p>Bezpečnosť</p>'
        service.merge_ai_values(item, content(), {'options': {'category_code': 'LEAF'}})
        self.assertEqual(item['values']['category_code'], 'LEAF')
        self.assertIn('Bezpečnosť', item['values']['description_html'])

    def test_missing_prices_and_duplicate_identities_still_block_parent_categories_are_allowed(self):
        rows = [row(sale_gross=None, category_code='ROOT'), row(2)]
        rows[1]['values']['eans'] = rows[0]['values']['eans']
        doc = document(rows)
        service.validate_rows(doc)
        self.assertIn('missing_price', rows[0]['errors'])
        self.assertIn('selection_identity_conflict', rows[1]['errors'])
        self.assertNotIn('category_must_be_leaf', rows[0]['errors'])
        self.assertIsNone(rows[0]['values']['sale_gross'])

    def test_inactive_parent_and_leaf_categories_can_be_saved_but_system_root_cannot(self):
        for code in ('ROOT', 'LEAF'):
            with self.subTest(code=code):
                doc = document([row(category_code=code)])
                for category in doc['categories']:
                    category['active'] = False
                service.validate_rows(doc)
                self.assertEqual(doc['rows'][0]['errors'], [])
                self.assertEqual(doc['rows'][0]['categories'][-1], {'code': code, 'main_yn': True})
        doc = document([row(category_code='MENU')])
        service.validate_rows(doc)
        self.assertIn('category_not_assignable', doc['rows'][0]['errors'])

    def test_no_inventory_or_nondecimal_cells_accepted(self):
        good = row()['values']
        for updates in ({'stock': 5}, {'sale_gross': '-1'}, {'purchase_net': 'NaN'}, {'vat_percent': '101'},
                        {'metadata': {'validation_required': '0'}}, {'images':['javascript:alert(1)']}):
            with self.subTest(updates=updates), self.assertRaises(ValidationError):
                ImportValues.model_validate({**good, **updates})

    def test_legacy_queue_cannot_confirm_a_staged_preview(self):
        with patch.object(catalog_import, '_path', return_value=object()), patch.object(catalog_import, '_load', return_value={'staging': {'draft_id': 'draft'}}):
            with self.assertRaises(CatalogError) as raised:
                catalog_import.queue_import('biketrek', 'a' * 32)
            self.assertEqual(raised.exception.code, 'import_staging_confirmation_required')


class ProductImportAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_completed_draft_exposes_verified_cached_links_and_keeps_ai_job_lookup(self):
        from inventory_hub.services import catalog
        item = row()
        item['ai_job_id'] = 'job-identifier'
        doc = {**document([item]), 'feed_key': 'products', 'publication': {'preview_id': 'original'}}
        draft = SimpleNamespace(id='draft', shop='biketrek', supplier='paul-lange', revision=3,
            status='saved', document=doc, created_at=None, updated_at=None)
        result = {'status': 'completed', 'items': [{'product_ids': [1], 'status': 'created'}]}
        job = SimpleNamespace(id='job-identifier', status='completed', context={})
        db = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [job])))
        async def links(db, shop, sources):
            sources[0].shop_url = 'https://shop.example/product'
            sources[0].shop_admin_url = 'https://admin.example/product/7'
        with patch.object(service, 'publication_result', return_value=result), \
             patch.object(service, 'target_result', return_value=result), \
             patch.object(ai_content, 'summary', return_value={'id': job.id}), \
             patch.object(catalog, 'attach_shop_links', side_effect=links):
            response = await service.public(db, draft)
        self.assertEqual(response['rows'][0]['publication_links'][0]['shop_admin_url'], 'https://admin.example/product/7')
        self.assertEqual(response['rows'][0]['publication_links'][0]['shop_url'], 'https://shop.example/product')
        self.assertEqual(response['rows'][0]['ai_job']['id'], job.id)
        self.assertEqual(list(db.scalars.call_args.args[0].compile().params.values()), [[job.id]])
        self.assertNotIn('shop_url', result['items'][0], 'Readback enrichment does not rewrite the durable journal')

    async def test_preview_splits_rows_and_categories_without_publishing_hub_only_rows(self):
        rows = [row(target_shops=['biketrek', 'xtrek'], target_category_codes={'xtrek': 'OUTDOOR'}),
                row(2, target_shops=[]), row(3, target_shops=['xtrek'], target_category_codes={'xtrek': 'OUTDOOR'})]
        doc = document(rows)
        rows[0]['ai_policy'] = {'job_id': 'ai', 'content_digest': 'approved', 'active_after_import': True}
        doc.update(target='biketrek', targets={'biketrek': 'biketrek', 'xtrek': 'xtrek'}, feed_key='products',
                   shops=[{'code': 'biketrek', 'categories': doc['categories']},
                          {'code': 'xtrek', 'categories': [{'code': 'OUTDOOR'}]}])
        doc['saved_hash'] = service.content_hash(doc)
        draft = SimpleNamespace(id='draft', shop='biketrek', supplier='paul-lange', revision=1, status='saved', document=doc)
        calls = []
        async def build(db, shop, request, *, frozen_products, staging):
            calls.append((shop, request, staging))
            result = {'preview_id': shop * 4, 'items': [{'product_ids': request.product_ids, 'status': 'ready'}], 'errors': []}
            return SimpleNamespace(model_dump=lambda **_: result)
        db = SimpleNamespace(flush=AsyncMock())
        with patch.object(service, 'publication_result', return_value=None), \
             patch.object(service, 'assert_ai_applied', AsyncMock()), \
             patch.object(service, 'public', AsyncMock(side_effect=lambda db, draft: draft.document)), \
             patch.object(catalog_import, 'shop_config', side_effect=lambda shop: shop), \
             patch.object(catalog_import, '_target', side_effect=lambda shop: shop), \
             patch.object(catalog_import, 'create_preview', side_effect=build):
            await service.preview(db, draft, SimpleNamespace(expected_revision=1))
        self.assertEqual([(shop, request.product_ids) for shop, request, _ in calls], [('biketrek', [1]), ('xtrek', [1, 3])])
        self.assertEqual(calls[0][2]['rows'][0]['values']['category_code'], 'LEAF')
        self.assertIn('ai_policy', calls[0][2]['rows'][0])
        self.assertNotIn('ai_policy', calls[1][2]['rows'][0])
        self.assertFalse(service.approved_visibility(rows, [1], shop='xtrek', primary_shop='biketrek'))
        self.assertTrue(all(row['values']['category_code'] == 'OUTDOOR' for row in calls[1][2]['rows']))
        self.assertEqual(draft.document['rows'][0]['values']['category_code'], 'LEAF')
        self.assertEqual(set(draft.document['publications']), {'biketrek', 'xtrek'})
        self.assertEqual([item['shop'] for item in draft.document['publication']['items']], ['biketrek', 'xtrek'])

    async def test_shop_executors_start_together_before_either_long_batch_finishes(self):
        both_started = asyncio.Event()
        started = []
        async def execute(shop, preview):
            started.append(shop)
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1)
        with patch.object(catalog_import, 'execute_import', side_effect=execute):
            await service.execute_publications([('biketrek', 'first'), ('xtrek', 'second')])
        self.assertEqual(set(started), {'biketrek', 'xtrek'})

    async def test_partial_publication_preview_replays_original_journals(self):
        result = {'status': 'completed', 'items': [{'status': 'created'}, {'status': 'failed'}]}
        draft = SimpleNamespace(revision=5)
        with patch.object(service, 'publication_result', return_value=result), \
             patch.object(service, 'public', AsyncMock(return_value='original')), \
             patch.object(catalog_import, 'create_preview', AsyncMock()) as create:
            self.assertEqual(await service.preview(None, draft, SimpleNamespace(expected_revision=5)), 'original')
        create.assert_not_called()

    async def test_zero_attempt_expiry_can_prepare_new_preview_and_keeps_old_journal_reference(self):
        doc = document()
        old = {'preview_id': 'old', 'items': [{'status': 'ready'}]}
        doc.update(target='biketrek', targets={'biketrek': 'biketrek'}, feed_key='products',
            publication=old, publications={'biketrek': old})
        doc['saved_hash'] = service.content_hash(doc)
        draft = SimpleNamespace(id='draft', shop='biketrek', supplier='paul-lange', revision=1, status='saved', document=doc)
        expired = {'status': 'failed', 'errors': ['import_source_availability_expired'],
            'items': [{'status': 'ready', 'errors': [], 'product_write_attempted': False}]}
        new = {'preview_id': 'new', 'items': [{'product_ids': [1], 'status': 'ready'}], 'errors': []}
        with patch.object(service, 'target_result', side_effect=lambda shop, preview: expired if preview['preview_id'] == 'old' else None), \
             patch.object(service, 'assert_ai_applied', AsyncMock()), \
             patch.object(service, 'public', AsyncMock(side_effect=lambda db, value: value.document)), \
             patch.object(catalog_import, 'shop_config', side_effect=lambda shop: shop), \
             patch.object(catalog_import, '_target', side_effect=lambda shop: shop), \
             patch.object(catalog_import, 'create_preview', AsyncMock(return_value=SimpleNamespace(model_dump=lambda **_: new))):
            await service.preview(SimpleNamespace(flush=AsyncMock()), draft, SimpleNamespace(expected_revision=1))
        self.assertEqual(draft.document['publications']['biketrek']['preview_id'], 'new')
        self.assertEqual(draft.document['previous_publications'][0]['previews'], {'biketrek': 'old'})
        self.assertEqual(expired['items'][0]['product_write_attempted'], False)

    async def test_ai_must_be_applied_or_explicitly_disabled_before_publication(self):
        item = row(ai_enabled=True)
        item['ai_job_id'] = 'ai'
        draft = SimpleNamespace(id='draft', document=document([item]))
        job = SimpleNamespace(id='ai', status='ready', context={'staging_id':'draft'})
        db = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [job])))
        for state in ('ready', 'review', 'blocked', 'generating'):
            job.status = state
            with self.subTest(state=state), self.assertRaises(CatalogError) as raised:
                await service.assert_ai_applied(db, draft)
            self.assertEqual(raised.exception.code, 'import_ai_not_applied')
        job.status = 'completed'
        await service.assert_ai_applied(db, draft)
        job.context['staging_id'] = 'different-draft'
        with self.assertRaises(CatalogError):
            await service.assert_ai_applied(db, draft)
        item['values']['ai_enabled'] = False
        db.scalars.reset_mock()
        await service.assert_ai_applied(db, draft)
        db.scalars.assert_not_called()

    async def test_completed_publication_is_read_only_and_repeated_preview_preserves_result(self):
        draft = SimpleNamespace(id='draft', revision=9, document={**document(), 'publication':{'preview_id':'preview'}})
        result = {'status':'completed', 'items':[{'product_ids':[1], 'status':'created'}]}
        request = SimpleNamespace(expected_revision=9, preview_id='preview')
        with patch.object(service, 'publication_result', return_value=result), \
             patch.object(service, 'public', AsyncMock(return_value={'publication_result':result})), \
             patch.object(catalog_import, 'create_preview', AsyncMock()) as prepare, \
             patch.object(catalog_import, 'queue_import') as queue:
            with self.assertRaises(CatalogError) as raised:
                service.assert_editable(draft)
            self.assertEqual(raised.exception.code, 'import_publication_finished')
            self.assertEqual((await service.preview(None, draft, request))['publication_result'], result)
            returned, run = await service.publish(None, draft, request)
            self.assertEqual(returned['publication_result'], result)
            self.assertFalse(run)
            self.assertEqual(draft.revision, 9)
            prepare.assert_not_called()
            queue.assert_not_called()

    async def test_staged_ai_cannot_import_fork_or_update_via_old_routes(self):
        job = SimpleNamespace(context={'staging_id': 'draft'}, revision=2)
        request = SimpleNamespace(expected_revision=2, action='import')
        for operation in (ai_content.action, ai_content.fork_job, ai_content_update.prepare, ai_content_update.confirm):
            with self.subTest(operation=operation.__name__), self.assertRaises(CatalogError) as raised:
                await operation(None, job, request)
            self.assertEqual(raised.exception.code, 'import_staging_confirmation_required')
        job.status, job.events, job.updated_at = 'preparing_import', [], None
        await ai_content.prepare_import(None, job)
        self.assertEqual(job.status, 'ready')

    async def test_unchecked_ai_result_is_not_applied_or_started(self):
        item = row(ai_enabled=False)
        item['ai_job_id'] = 'j' * 32
        draft = SimpleNamespace(revision=2, document=document([item]))
        db = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [])))
        with patch.object(service, 'publication_result', return_value=None):
            for operation, code in ((service.apply_ai, 'import_ai_not_ready'), (service.start_ai, 'import_ai_no_estimate')):
                with self.subTest(operation=operation.__name__), self.assertRaises(CatalogError) as raised:
                    await operation(db, draft, SimpleNamespace(expected_revision=2))
                self.assertEqual(raised.exception.code, code)
                statement = db.scalars.call_args.args[0]
                self.assertEqual(list(statement.compile().params.values()), [[]])
        self.assertEqual(draft.document['rows'][0]['values']['name'], item['values']['name'])

    async def test_bulk_family_edit_is_atomic_and_cas_protected(self):
        rows = [row(), row(2)]
        for item in rows:
            item.update(group_key='PL-G-X', is_variant=True)
        draft = SimpleNamespace(revision=2, supplier='paul-lange', status='draft', document=document(rows))
        request = DraftPatch(expected_revision=1, rows=[{'id':1,'values':{'brand':'New'}}])
        with self.assertRaises(CatalogError) as raised:
            await service.patch_rows(None, draft, request)
        self.assertEqual(raised.exception.code, 'import_draft_changed')
        request.expected_revision = 2
        request.rows.append(type(request.rows[0])(id=2, values={'brand':'Conflicting'}))
        with patch.object(service, 'publication_result', return_value=None), patch.object(service, 'supplier_config', return_value={'product_code_prefix':'PL-'}), patch('inventory_hub.supplier_prefix.get_supplier_prefix', return_value='PL-'):
            with self.assertRaises(CatalogError) as raised:
                await service.patch_rows(None, draft, request)
        self.assertEqual(raised.exception.code, 'import_family_edit_conflict')
        self.assertEqual(draft.document['rows'][0]['values']['brand'], 'TEST')
