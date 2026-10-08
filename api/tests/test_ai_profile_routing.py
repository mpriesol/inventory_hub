"""Content profiles survive import staging independently of shop placement."""
import json
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from catalog_fixtures import product
from test_ai_content import content
from test_product_import import document, row
from inventory_hub.ai_content_models import AiJob
from inventory_hub.ai_content_types import BatchRequest, CategoryProfile, Rule, RuleBook, Scope
from inventory_hub.product_import_types import DraftAiRequest, DraftPatch
from inventory_hub.services import ai_category, ai_content, ai_content_provider, product_import
from inventory_hub.services.catalog import CatalogError


def book():
    return RuleBook(rules=[
        Rule(id='shared', name='Shared', instructions='Preserve facts and write Slovak.'),
        Rule(id='tube-details', name='Tube rules', scope=Scope(category='inner_tubes'),
             instructions='TUBE_MATRIX_REQUIRED. Keep ETRTO pairs and valve identity.'),
        Rule(id='helmet-details', name='Helmet rules', scope=Scope(category='helmets'),
             instructions='HELMET_ONLY. Describe helmet fit.')], categories=[
        CategoryProfile(id='general', name='Všeobecné'),
        CategoryProfile(id='inner_tubes', name='Duše', shop_categories={'biketrek': 'TUBES'}),
        CategoryProfile(id='helmets', name='Prilby', shop_categories={'biketrek': 'HELMETS'})])


TREE = [{'code': 'ROOT', 'names': {'sk': 'Komponenty'}},
        {'code': 'TUBES', 'parent_code': 'ROOT', 'names': {'sk': 'Duše'}},
        {'code': 'TEMP', 'parent_code': 'ROOT', 'names': {'sk': 'Dočasná kategória'}}]


class ProfileRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def prepare(self, requested, category):
        saved = []
        db = SimpleNamespace(execute=AsyncMock(), get=AsyncMock(return_value=None),
            scalar=AsyncMock(return_value=1), add=Mock(side_effect=saved.append), flush=AsyncMock())
        request = BatchRequest(request_id=uuid4(), supplier='paul-lange', product_ids=[1],
            ai_product_ids=[1], category_profiles={1: requested}, research='feed_only',
            targets=[{'shop': 'biketrek', 'options': {'category_code': category},
                      'policy': {'show_cost_estimate': True}}])
        with patch.object(ai_content.rules, 'published', AsyncMock(return_value=SimpleNamespace(id=15, book=book().model_dump()))), \
             patch.object(ai_content, 'effective_model', AsyncMock(return_value='gpt-5.6-sol')), \
             patch.object(ai_content.catalog_import, 'shop_config', return_value={}), \
             patch.object(ai_content.catalog_import, '_target', return_value='shop-target'), \
             patch.object(ai_content.catalog_import, 'cached_import_options', return_value={'categories': TREE}) as categories, \
             patch.object(ai_content.provider, 'estimate', return_value=Decimal('.10')):
            await ai_content.create_batch(db, request, frozen_products=[product()], staging_id='draft')
        return next(item.context for item in saved if isinstance(item, AiJob)), categories

    async def test_mapped_auto_profile_avoids_paid_classification_and_keeps_only_relevant_rules(self):
        context, categories = await self.prepare('auto', 'TUBES')
        self.assertEqual(context['category_profile'], 'inner_tubes')
        self.assertEqual(context['category_profile_source'], 'category_mapping')
        self.assertEqual(context['options']['category_code'], 'TUBES')
        self.assertNotIn('classification_catalog', context)
        categories.assert_called_once()
        body = ai_content_provider.request_body(context)
        self.assertIn('TUBE_MATRIX_REQUIRED', body['input'])
        self.assertNotIn('HELMET_ONLY', body['input'])

    async def test_explicit_profile_survives_unmapped_placement(self):
        context, categories = await self.prepare('inner_tubes', 'TEMP')
        self.assertEqual(context['category_profile'], 'inner_tubes')
        self.assertEqual(context['category_profile_source'], 'explicit')
        self.assertEqual(context['options']['category_code'], 'TEMP')
        self.assertNotIn('classification_catalog', context)
        categories.assert_not_called()
        self.assertIn('TUBE_MATRIX_REQUIRED', ai_content_provider.request_body(context)['input'])

    async def test_explicit_general_profile_is_not_overridden_by_category_mapping(self):
        context, categories = await self.prepare('general', 'TUBES')
        self.assertEqual(context['category_profile'], 'general')
        self.assertEqual(context['category_profile_source'], 'explicit')
        self.assertNotIn('classification_catalog', context)
        categories.assert_not_called()

    async def test_unmapped_placement_classifies_profile_without_reassigning_product(self):
        context, _ = await self.prepare('auto', 'TEMP')
        self.assertEqual(context['classification_mode'], 'profile')
        body = ai_content_provider.request_body(context, 'classification')
        self.assertEqual([c['code'] for c in json.loads(body['input'])['choices']], ['TEMP'])
        self.assertIn('Kategóriu už vybral', body['instructions'])
        final = ai_category.apply(context, book(), {'category_code': 'TEMP', 'profile_id': 'inner_tubes',
            'confident': True, 'reason': 'Product facts identify a tube.'})
        self.assertEqual(final['options']['category_code'], 'TEMP')
        self.assertEqual(final['category_profile'], 'inner_tubes')
        self.assertIn('TUBE_MATRIX_REQUIRED', ai_content_provider.request_body(final)['input'])
        self.assertNotIn('HELMET_ONLY', ai_content_provider.request_body(final)['input'])
        with self.assertRaises(CatalogError):
            ai_category.apply(context, book(), {'category_code': 'TUBES', 'profile_id': 'inner_tubes',
                'confident': True, 'reason': 'Must not move a manually placed product.'})

    async def test_missing_placement_still_selects_leaf_and_ancestors(self):
        context, _ = await self.prepare('auto', None)
        self.assertEqual(context['classification_mode'], 'category_and_profile')
        final = ai_category.apply(context, book(), {'category_code': 'TUBES', 'profile_id': 'inner_tubes',
            'confident': True, 'reason': 'Tube category.'})
        self.assertEqual(final['options']['category_code'], 'TUBES')
        self.assertEqual(ai_category.category_chain(TREE, final['options']['category_code']),
            [{'code': 'ROOT', 'main_yn': False}, {'code': 'TUBES', 'main_yn': True}])

    async def test_automatic_parent_selection_still_descends_to_leaf(self):
        context, _ = await self.prepare('auto', 'ROOT')
        self.assertEqual(context['classification_mode'], 'category_and_profile')
        self.assertNotIn('ROOT', [c['code'] for c in context['classification_catalog']['choices']])
        final = ai_category.apply(context, book(), {'category_code': 'TUBES', 'profile_id': 'inner_tubes',
            'confident': True, 'reason': 'Choose the leaf below the selected parent.'})
        self.assertEqual(final['options']['category_code'], 'TUBES')
        item = row(category_code='ROOT')
        item['manual_fields'] = ['category_code']
        product_import.merge_ai_values(item, content(), final)
        self.assertEqual(item['values']['category_code'], 'TUBES')
        changed = row(category_code='ROOT')
        changed['values']['category_code'] = 'TEMP'
        with self.assertRaises(CatalogError):
            product_import.merge_ai_values(changed, content(), final)

    async def test_nearest_ancestor_profile_and_ambiguous_leaf_mapping(self):
        rules = book()
        rules.categories[1].shop_categories = {'biketrek': 'ROOT'}
        self.assertEqual(ai_category.mapped_profile(rules, 'biketrek', TREE, 'TEMP'), 'inner_tubes')
        rules.categories[2].shop_categories = {'biketrek': 'TEMP'}
        self.assertEqual(ai_category.mapped_profile(rules, 'biketrek', TREE, 'TEMP'), 'helmets')
        rules.categories.append(CategoryProfile(id='tyres', name='Plášte', shop_categories={'biketrek': 'TEMP'}))
        self.assertEqual(ai_category.mapped_profile(rules, 'biketrek', TREE, 'TEMP'), 'general')

    async def test_draft_forwards_explicit_profile_and_historical_rows_default_to_auto(self):
        first, second = row(ai_enabled=True, ai_category_profile='inner_tubes'), row(2, ai_enabled=True)
        second['values'].pop('ai_category_profile')  # Saved pre-upgrade draft.
        draft = SimpleNamespace(id='draft', revision=2, supplier='paul-lange', shop='biketrek', status='draft',
            document={**document([first, second]), 'feed_key': 'products'})
        db = SimpleNamespace(flush=AsyncMock())
        prepare = AsyncMock(return_value=[{'id': 'job'}])
        with patch.object(product_import, 'publication_result', return_value=None), \
             patch.object(ai_content, 'create_batch', prepare), \
             patch.object(product_import, 'public', AsyncMock(return_value={})):
            await product_import.prepare_ai(db, draft, DraftAiRequest(expected_revision=2, research='feed_only'))
        self.assertEqual(prepare.await_args_list[0].args[1].category_profiles, {1: 'inner_tubes'})
        self.assertEqual(prepare.await_args_list[1].args[1].category_profiles, {2: 'auto'})

    async def test_changing_profile_cancels_unstarted_job_and_rejects_stale_result(self):
        item = row(ai_enabled=True)
        item['ai_job_id'] = 'old-job'
        draft = SimpleNamespace(id='draft', revision=2, supplier='paul-lange', status='draft', document=document([item]))
        old = SimpleNamespace(status='queued', revision=1, reserved_usd=Decimal('1'), events=[])
        db = SimpleNamespace(flush=AsyncMock(), scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [old])))
        request = DraftPatch(expected_revision=2, rows=[{'id': 1, 'values': {'ai_category_profile': 'inner_tubes'}}])
        with patch.object(product_import, 'publication_result', return_value=None), \
             patch.object(product_import, 'supplier_config', return_value={'product_code_prefix': 'PL-'}), \
             patch('inventory_hub.supplier_prefix.get_supplier_prefix', return_value='PL-'), \
             patch.object(product_import, 'public', AsyncMock(return_value={})):
            await product_import.patch_rows(db, draft, request)
        self.assertEqual((old.status, old.reserved_usd), ('cancelled', 0))
        changed = draft.document['rows'][0]
        with self.assertRaises(CatalogError) as raised:
            product_import.merge_ai_values(changed, content(), {})
        self.assertEqual(raised.exception.code, 'import_ai_identity_changed')
        old.id, old.status, old.context = 'old-job', 'completed', {'staging_id': 'draft'}
        self.assertFalse(product_import.ai_job_import_status(old, draft, None)['ai_applied'])
        with self.assertRaises(CatalogError) as raised:
            await product_import.assert_ai_applied(db, draft)
        self.assertEqual(raised.exception.code, 'import_ai_not_applied')


if __name__ == '__main__':
    unittest.main()
