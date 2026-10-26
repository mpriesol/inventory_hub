"""Explicit import identity choices and atomic link-only saves; synthetic data."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy import select, func, text

import test_product_import_db as fixture
from test_product_import import row, document
from inventory_hub.db_models import Product, ProductIdentifier, SupplierProduct, IdentifierType
from inventory_hub.db_models_ext import ProductSupplySource, ShopProduct
from inventory_hub.product_editor_models import ProductEditorOverride
from inventory_hub.product_import_types import DraftCreate, DraftRevision, DraftPatch, DraftIdentityDecisions
from inventory_hub.services import product_import as imports, product_import_identity as identity
from inventory_hub.services.catalog import CatalogError
from inventory_hub.services.product_identity import IdentityIndex


class ImportIdentityUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_candidate_evidence_blocks_creation_even_without_resolver_match(self):
        staged = document()
        draft = SimpleNamespace(id='draft', revision=1, supplier='paul-lange', document=staged)
        product = SimpleNamespace(id=99, sku='EXISTING-OTHER-SKU', is_active=True)
        index = IdentityIndex(0, [product], [], [])
        db = SimpleNamespace(scalar=AsyncMock(return_value=SimpleNamespace(id=1)))
        for reason in ('exact_sku', 'supplier_sku'):
            candidate = {'product_id': 99, 'sku': product.sku, 'name': 'Known card', 'brand': '', 'eans': [],
                'attributes': [], 'manual_eans': None, 'is_active': True, 'reasons': [reason], 'warnings': []}
            with self.subTest(reason=reason), \
                 patch.object(identity, 'find_candidates', AsyncMock(return_value={'1': {'candidates': [candidate], 'truncated': False}})), \
                 patch.object(identity, 'load_identity_index', AsyncMock(return_value=index)):
                review, _ = await identity.build(db, draft)
            self.assertIsNone(review['rows'][0]['matched_product_id'])
            self.assertFalse(review['rows'][0]['can_create'])
            self.assertTrue(review['rows'][0]['candidates'][0]['can_link'])

    async def test_automatic_ai_completion_stops_before_save_when_identity_review_is_pending(self):
        item = row(ai_enabled=True, target_shops=[])
        item['ai_job_id'] = 'job'
        staged = document([item])
        staged['automation'] = {'version': 1, 'job_ids': ['job'], 'paused': False, 'auto_publish_requested': False}
        draft = SimpleNamespace(id='draft', shop='biketrek', revision=1, document=staged)
        job = SimpleNamespace(id='job', context={'staging_id': 'draft', 'staging_automation_version': 1,
            'approval': 'policy', 'resolved': {'policy': {'confirm_import': False}}}, error=None)
        db = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [job])))
        with patch.object(imports, 'get_draft', AsyncMock(return_value=draft)), \
             patch.object(imports, 'publication_result', return_value=None), \
             patch.object(imports, 'apply_ai', AsyncMock()), \
             patch.object(imports, 'applied_ai', return_value=True), \
             patch.object(imports, 'assert_ai_applied', AsyncMock()), \
             patch.object(identity, 'summary', AsyncMock(return_value={'identity_review_required': True})), \
             patch.object(imports.ai_content, 'event') as event, \
             patch.object(imports, 'save', AsyncMock()) as save, \
             patch.object(imports, 'preview', AsyncMock()) as preview:
            await imports.advance_ai(db, job)
        save.assert_not_awaited()
        preview.assert_not_awaited()
        self.assertEqual(job.error, 'import_identity_review_required')
        self.assertEqual(event.call_args.args[1], 'import_blocked')

    def test_link_only_rows_skip_shop_and_merchandising_validation_without_changing_shared_values(self):
        item = row(target_shops=['biketrek', 'xtrek'], sale_gross=None, vat_percent=None)
        item['identity_decision'] = {'action': 'link', 'product_id': 91}
        before = deepcopy(item['values'])
        imports.validate_rows(document([item]))
        self.assertEqual(item['errors'], [])
        self.assertEqual(imports.row_targets(item, 'biketrek'), [])
        self.assertEqual(item['values'], before)
        self.assertIn('existing_card_link_only', item['warnings'])

    async def test_explicit_link_only_on_automatic_match_uses_reviewed_evidence(self):
        from inventory_hub.services import supplier_card_links
        item = row()
        item['identity_decision'] = {'action': 'link', 'product_id': 91, 'link_hash': 'reviewed-hash'}
        staged = document([item])
        reviewed = {'rows': [{'row_id': item['id'], 'status': 'linked', 'matched_product_id': 91,
            'decision': {'action': 'link', 'product_id': 91}, 'source': {},
            'candidates': [{'product_id': 91, 'can_link': True}]}]}
        with patch.object(identity, 'build', AsyncMock(return_value=(reviewed, {}))), \
             patch.object(identity, '_inspect_choice', AsyncMock()) as inspect, \
             patch.object(supplier_card_links, 'apply_link', AsyncMock()) as apply:
            linked = await identity.guard_and_link(None, SimpleNamespace(), staged)
        inspect.assert_not_awaited()
        apply.assert_awaited_once_with(None, 91, item['id'], expected_hash='reviewed-hash')
        self.assertEqual(linked, {item['id']: 91})
        self.assertTrue(item['identity_link_only'])


@unittest.skipUnless(fixture.catalog_db.TEST_URL, 'Dedicated localhost *_catalog_test database required')
class ImportIdentityDatabaseTests(unittest.IsolatedAsyncioTestCase):
    # Reuse setup/helpers only; inheriting the test class would run its entire
    # existing suite twice and invalidate the module-preserving CI inventory.
    asyncSetUp = fixture.ProductImportDatabaseTests.asyncSetUp
    asyncTearDown = fixture.ProductImportDatabaseTests.asyncTearDown
    write_feed = fixture.ProductImportDatabaseTests.write_feed
    refresh = fixture.ProductImportDatabaseTests.refresh
    request = fixture.ProductImportDatabaseTests.request
    create = fixture.ProductImportDatabaseTests.create
    approve_fixture_identity = fixture.ProductImportDatabaseTests.approve_fixture_identity
    mutate = fixture.ProductImportDatabaseTests.mutate

    async def existing(self, *, exact=True, name=None):
        async with self.sessions() as db:
            source = (await fixture.catalog.selected_products(db, 'paul-lange', 'products', [self.ids[0]]))[0]
            product = Product(sku=source.shop_code if exact else 'ORIGINAL-SKU',
                              name=name or source.name, brand=source.brand)
            db.add(product)
            await db.flush()
            db.add(ProductEditorOverride(product_id=product.id, revision=3,
                data={'common': {'name': name or source.name}, 'variant': {'sale_price_gross': '88.00'}}))
            await db.commit()
            return product.id, source

    async def review(self, draft):
        return await self.mutate(draft, identity.review)

    async def choose(self, draft, decisions):
        review = await self.review(draft)
        return await self.mutate(draft, identity.decisions, DraftIdentityDecisions,
                                 review_hash=review['review_hash'], decisions=decisions)

    async def business(self):
        async with self.sessions() as db:
            return {table: await db.scalar(text(f"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY t.id), '[]'::jsonb) FROM {table} t"))
                for table in ('products', 'product_identifiers', 'product_supply_sources', 'stock_balances', 'stock_movements', 'shop_products')}

    async def test_exact_existing_card_requires_choice_and_link_only_save_preserves_content_and_replays(self):
        pid, source = await self.existing()
        before = await self.business()
        draft = await self.create()
        self.assertTrue(draft['identity_review_required'])
        with self.assertRaises(CatalogError) as blocked:
            await self.mutate(draft, imports.save)
        self.assertEqual(blocked.exception.code, 'import_identity_review_required')
        review = await self.review(draft)
        self.assertFalse(review['rows'][0]['can_create'])
        chosen = await self.choose(draft, [{'row_id': self.ids[0], 'action': 'link', 'product_id': pid}])
        self.assertEqual(await self.business(), before, 'Review choices alone do not persist associations')
        saved = await self.mutate(chosen, imports.save)
        self.assertFalse(saved['identity_review_required'])
        self.assertEqual(saved['rows'][0]['hub_product_id'], pid)
        self.assertEqual(saved['rows'][0]['values']['target_shops'], [])
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(Product)), 1)
            self.assertEqual((await db.get(Product, pid)).sku, source.shop_code)
            override = await db.get(ProductEditorOverride, pid)
            self.assertEqual((override.revision, override.data['variant']['sale_price_gross']), (3, '88.00'))
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductSupplySource)), 1)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ShopProduct)), 0)
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM stock_movements')), 0)
        snapshot = await self.business()
        await self.mutate(saved, imports.save)
        self.assertEqual(await self.business(), snapshot)
        self.assertEqual(self.client.sent, [])

    async def test_name_similarity_needs_explicit_create_dismissal(self):
        original, _ = await self.existing(exact=False)
        draft = await self.create()
        review = await self.review(draft)
        self.assertTrue(review['rows'][0]['can_create'])
        self.assertIn('similar_name', review['rows'][0]['candidates'][0]['reasons'])
        with self.assertRaises(CatalogError):
            await self.mutate(draft, imports.save)
        chosen = await self.choose(draft, [{'row_id': self.ids[0], 'action': 'create'}])
        saved = await self.mutate(chosen, imports.save)
        self.assertNotEqual(saved['rows'][0]['hub_product_id'], original)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(Product)), 2)

    async def automatic_existing(self):
        self.write_feed(fixture.xml_item(ean='5901234123457'))
        await self.refresh()
        pid, source = await self.existing()
        async with self.sessions() as db:
            db.add(ProductIdentifier(product_id=pid, identifier_type=IdentifierType.ean, value=source.eans[0]))
            await db.commit()
        return pid

    async def test_automatic_match_can_explicitly_choose_link_only_and_preserves_standalone_card(self):
        pid = await self.automatic_existing()
        draft = await self.create()
        reviewed = await self.review(draft)
        self.assertEqual(reviewed['rows'][0]['status'], 'linked')
        before = await self.business()
        chosen = await self.choose(draft, [{'row_id': self.ids[0], 'action': 'link', 'product_id': pid}])
        self.assertEqual(await self.business(), before)
        self.assertEqual((await self.review(chosen))['rows'][0]['decision'], {'action': 'link', 'product_id': pid})
        # Explicit link-only excludes the standalone card from canonical
        # grouping, including when a supplier puts it in a variant family.
        with patch.object(imports.catalog_import, 'canonical_products', wraps=imports.catalog_import.canonical_products) as canonical:
            saved = await self.mutate(chosen, imports.save)
        canonical.assert_awaited_once()
        self.assertEqual(canonical.call_args.args[1], [])
        self.assertEqual(saved['rows'][0]['hub_product_id'], pid)
        self.assertEqual(saved['rows'][0]['values']['target_shops'], [])
        after = await self.business()
        for table in ('products', 'stock_balances', 'stock_movements', 'shop_products'):
            self.assertEqual(after[table], before[table])
        async with self.sessions() as db:
            override = await db.get(ProductEditorOverride, pid)
            self.assertEqual((override.revision, override.data['variant']['sale_price_gross']), (3, '88.00'))
        self.assertEqual(self.client.sent, [])

    async def test_automatic_match_cannot_be_created_or_redirected_and_explicit_choice_stays_fresh(self):
        pid = await self.automatic_existing()
        draft = await self.create()
        for choice in ({'action': 'create'}, {'action': 'link', 'product_id': pid + 1}):
            with self.subTest(choice=choice), self.assertRaises(CatalogError) as blocked:
                await self.choose(draft, [{'row_id': self.ids[0], **choice}])
            self.assertEqual(blocked.exception.code, 'import_identity_decision_invalid')
        chosen = await self.choose(draft, [{'row_id': self.ids[0], 'action': 'link', 'product_id': pid}])
        async with self.sessions() as db:
            source = await db.get(SupplierProduct, self.ids[0])
            source.is_active = False
            await db.commit()
        before = await self.business()
        with self.assertRaises(CatalogError):
            await self.mutate(chosen, imports.save)
        self.assertEqual(await self.business(), before, 'Explicit link cannot refresh its own evidence through automatic matching')

    async def test_candidate_change_after_choice_requires_new_review(self):
        pid, _ = await self.existing()
        chosen = await self.choose(await self.create(), [{'row_id': self.ids[0], 'action': 'link', 'product_id': pid}])
        async with self.sessions() as db:
            override = await db.get(ProductEditorOverride, pid)
            override.data = {'common': {'name': 'Changed after review'}}
            await db.commit()
        before = await self.business()
        with self.assertRaises(CatalogError) as blocked:
            await self.mutate(chosen, imports.save)
        self.assertEqual(blocked.exception.code, 'import_identity_review_required')
        self.assertEqual(await self.business(), before)

    async def test_source_change_after_choice_cannot_use_fresh_hash_to_authorize_itself(self):
        pid, _ = await self.existing()
        chosen = await self.choose(await self.create(), [{'row_id': self.ids[0], 'action': 'link', 'product_id': pid}])
        async with self.sessions() as db:
            source = await db.get(SupplierProduct, self.ids[0])
            source.is_active = False
            await db.commit()
        before = await self.business()
        with self.assertRaises(CatalogError):
            await self.mutate(chosen, imports.save)
        self.assertEqual(await self.business(), before)

    async def test_existing_supplier_source_owner_without_alias_cannot_create_duplicate(self):
        pid, _ = await self.existing(exact=False)
        async with self.sessions() as db:
            db.add(ProductSupplySource(product_id=pid, supplier_product_id=self.ids[0]))
            await db.commit()
        draft = await self.create()
        review = await self.review(draft)
        self.assertIsNone(review['rows'][0]['matched_product_id'])
        self.assertFalse(review['rows'][0]['can_create'])
        self.assertIn('supplier_sku', review['rows'][0]['candidates'][0]['reasons'])
        with self.assertRaises(CatalogError) as blocked:
            await self.choose(draft, [{'row_id': self.ids[0], 'action': 'create'}])
        self.assertEqual(blocked.exception.code, 'import_identity_decision_invalid')

    async def test_handled_later_failure_rolls_back_earlier_link_even_if_caller_commits(self):
        pid, _ = await self.existing()
        draft = await self.create(DraftCreate(request_id=uuid4(), supplier='paul-lange', shop='biketrek',
            product_ids=self.ids[:2], options={'category_code': 'K-TEST'}))
        draft = await self.mutate(draft, imports.patch_rows, DraftPatch,
            rows=[{'id': self.ids[1], 'values': {'description_html': '<script>alert(1)</script>'}}])
        chosen = await self.choose(draft, [{'row_id': self.ids[0], 'action': 'link', 'product_id': pid},
                                         {'row_id': self.ids[1], 'action': 'create'}])
        before = await self.business()
        async with self.sessions() as db:
            current = await imports.get_draft(db, chosen['id'], lock=True)
            with self.assertRaises(CatalogError) as blocked:
                await imports.save(db, current, DraftRevision(expected_revision=chosen['revision']))
            self.assertEqual(blocked.exception.code, 'import_invalid_rows')
            await db.commit()  # AI worker records the handled error in this transaction.
        self.assertEqual(await self.business(), before)

    async def test_conflicting_sku_and_ean_owners_are_reviewable_but_cannot_be_duplicated(self):
        self.write_feed(fixture.xml_item(ean='5901234123457'))
        await self.refresh()
        pid, source = await self.existing()
        async with self.sessions() as db:
            other = Product(sku='OTHER-EAN-OWNER', name='Unrelated item')
            db.add(other)
            await db.flush()
            db.add(ProductIdentifier(product_id=other.id, identifier_type=IdentifierType.ean, value=source.eans[0]))
            await db.commit()
        draft = await self.create()
        review = await self.review(draft)
        self.assertEqual(review['rows'][0]['status'], 'conflict')
        self.assertFalse(review['rows'][0]['can_create'])
        self.assertTrue(all(not c['can_link'] for c in review['rows'][0]['candidates']))
        with self.assertRaises(CatalogError):
            await self.mutate(draft, imports.save)
