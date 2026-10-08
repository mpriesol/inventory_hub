"""Import snapshots preserve money, manual work and the existing write boundaries."""
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

    def test_missing_prices_duplicate_identities_and_parent_category_block(self):
        rows = [row(sale_gross=None, category_code='ROOT'), row(2)]
        rows[1]['values']['eans'] = rows[0]['values']['eans']
        doc = document(rows)
        service.validate_rows(doc)
        self.assertIn('missing_price', rows[0]['errors'])
        self.assertIn('selection_identity_conflict', rows[1]['errors'])
        self.assertIn('category_must_be_leaf', rows[0]['errors'])
        self.assertIsNone(rows[0]['values']['sale_gross'])

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
