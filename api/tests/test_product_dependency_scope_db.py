"""Synthetic PostgreSQL scope, namespace, drift and phantom-fence regressions."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import select, text

import test_product_consolidation_db as fixture
from inventory_hub import config_io
from inventory_hub.ai_content_models import AiBatch, AiJob
from inventory_hub.catalog_types import CatalogPrices, CatalogProduct, ShopImportOptions
from inventory_hub.db_models import Product, Shop, Supplier, SupplierFeed, SupplierProduct
from inventory_hub.db_models_ext import ShopProduct
from inventory_hub.product_import_models import ProductImportDraft
from inventory_hub.product_import_types import ImportValues
from inventory_hub.product_consolidation_models import ProductCanonicalAlias, ProductConsolidation
from inventory_hub.services import product_consolidation as service, product_links as links
from inventory_hub.services import product_dependency_scan as scan, product_dependency_scope as scope
from inventory_hub.services import product_dependency_atomer_scope as atomer_scope
from inventory_hub.services import atomer_catalog_engine as atomer_engine
from inventory_hub.services import product_dependency_journals as journals
from inventory_hub.services import ai_content, product_import, catalog_import


def document(source_id=700, leaf='SYN-UNRELATED', parent='SYN-KEEP', *, variant=True):
    source = CatalogProduct(id=source_id, supplier='scope-fixture', code=leaf + '-SUPPLIER', shop_code=leaf,
        name='Synthetic source item', prices=CatalogPrices(currency='EUR'),
        group_code='SYN-GROUP' if variant else None, canonical_parent_code=parent if variant else None,
        variant_relationship='explicit' if variant else 'not_provided')
    values = ImportValues(code=leaf, supplier_code=source.code, name=source.name)
    row = {'id': source_id, 'group_key': parent if variant else leaf, 'is_variant': variant,
        'source': source.model_dump(mode='json'), 'values': values.model_dump(mode='json'),
        'canonical_code': leaf, 'hub_product_id': None, 'manual_fields': [], 'provenance': {}}
    return {'feed_key': 'products', 'run_id': None, 'options': ShopImportOptions().model_dump(mode='json'),
        'rows': [row], 'publication': None, 'shops': [{'code': 'biketrek', 'name': 'SYN-KEEP', 'categories': []}],
        'categories': [], 'targets': {'biketrek': 'a' * 64}, 'target': 'a' * 64, 'events': []}


@unittest.skipUnless(fixture.TEST_URL, 'Dedicated localhost *_catalog_test database required')
class ProductDependencyScopeDatabaseTests(unittest.IsolatedAsyncioTestCase):
    transaction = fixture.ProductConsolidationDatabaseTests.transaction
    leaf = fixture.ProductConsolidationDatabaseTests.leaf
    discover = fixture.ProductConsolidationDatabaseTests.discover
    verify = fixture.ProductConsolidationDatabaseTests.verify
    absence = fixture.ProductConsolidationDatabaseTests.absence
    code_absence = fixture.ProductConsolidationDatabaseTests.code_absence
    preview = fixture.ProductConsolidationDatabaseTests.preview
    apply = fixture.ProductConsolidationDatabaseTests.apply
    stock = fixture.ProductConsolidationDatabaseTests.stock
    request = fixture.ProductConsolidationDatabaseTests.request

    async def asyncSetUp(self):
        await fixture.ProductConsolidationDatabaseTests.asyncSetUp(self)
        async with self.transaction() as db:
            db.add(Supplier(code='scope-fixture', name='Synthetic scope supplier'))

    async def asyncTearDown(self):
        await fixture.ProductConsolidationDatabaseTests.asyncTearDown(self)

    async def add_draft(self, doc=None):
        raw = deepcopy(doc or document())
        identifier = uuid4().hex
        async with self.transaction() as db:
            db.add(ProductImportDraft(id=identifier, request_hash='d' * 64, revision=1,
                supplier='scope-fixture', shop='biketrek', status='draft', document=raw))
        return identifier, raw

    def report(self, preview):
        return preview['dependencies']['facts']['typed_catalog_scope']

    async def add_ai(self, *, staging=True, linked=True, second_target=False):
        job_id, doc = uuid4().hex, document()
        row = doc['rows'][0]
        if linked:
            row['ai_job_id'] = job_id
            row['ai_baseline'] = deepcopy(row['values'])
        if second_target:
            doc['rows'].append(document(source_id=701, leaf='SYN-OLD', variant=False)['rows'][0])
        draft_id, doc = await self.add_draft(doc)
        effective = product_import.to_source(row)
        ctx = {'source_kind': 'import_draft', 'shop': 'biketrek', 'supplier': 'scope-fixture',
            'code': 'SYN-KEEP', 'product_ids': [row['id']], 'facts': ai_content.facts([effective]),
            'source_digest': ai_content.source_digest([effective]), 'options': doc['options']}
        ctx.update(ai_content.provider.generation_settings('gpt-6-luna'))
        if staging:
            ctx['staging_id'] = draft_id
        async with self.transaction() as db:
            db.add(AiBatch(id=job_id, request_hash='e' * 64)); await db.flush()
            db.add(AiJob(id=job_id, batch_id=job_id, kind='product', revision=1, status='completed', context=ctx))
        return draft_id, job_id, ctx

    def journal(self, *, source_id=700, leaf='SYN-UNRELATED', state='invalid', attempted=False):
        identifier = uuid4().hex
        row = document(source_id=source_id, leaf=leaf)['rows'][0]
        item = {'code': 'SYN-KEEP', 'name': 'Synthetic item', 'product_ids': [source_id], 'status': state,
            'existing_product_ids': [], 'payload': {'code': 'SYN-KEEP', 'active_yn': False,
                'variants': [{'code': leaf, 'active_yn': False}]}, 'errors': [], 'warnings': [],
            'product_write_attempted': attempted}
        value = {'preview': {'preview_id': identifier, 'shop': 'biketrek', 'supplier': 'scope-fixture',
            'items': [deepcopy(item)], 'price_lines': [{'product_id': source_id, 'code': leaf,
                'shop_matches': [{'matched_by': 'parent_code', 'value': 'SYN-KEEP', 'code': 'SYN-UNRELATED-SIBLING',
                                  'parent_code': 'SYN-KEEP', 'remote_product_id': '9001'}]}]},
            'sources': [row['source']], 'result': {'preview_id': identifier, 'shop': 'biketrek', 'status': 'failed',
                'items': [deepcopy(item)], 'updated_at': links.now().isoformat()}}
        path = config_io.shop_path('biketrek').parent / 'catalog-imports' / (identifier + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path, value

    async def test_display_label_and_create_family_context_are_disjoint_and_full_row_frozen(self):
        doc = document()
        async with self.transaction() as db:
            await product_import.prepare_sku_rows(db, SimpleNamespace(supplier='scope-fixture'), doc)
        product_import.validate_rows(doc)
        self.assertIn('categories', doc['rows'][0])
        self.assertIn('supplier_id', doc)
        identifier, _ = await self.add_draft(doc)
        before = await self.stock()
        preview = await self.preview()
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        record = next(row for row in self.report(preview)['records'] if row['row_identity'].get('id') == identifier)
        self.assertEqual(record['classification'], 'context_only')
        self.assertTrue(record['row_hash'])
        self.assertIn('known_shop_display_label', {p['reason'] for p in record['context_paths']})
        result = await self.apply(preview)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(await self.stock(), before)

    async def test_same_word_at_leaf_code_remains_a_dependency(self):
        await self.add_draft(document(leaf='SYN-KEEP', variant=False))
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        self.assertIn('product_import_drafts', {b.get('table') for b in preview['dependencies']['blockers']})

    async def test_supplier_source_number_equal_to_hub_number_is_not_a_hub_reference(self):
        await self.add_draft(document(source_id=self.retired))
        preview = await self.preview()
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])

    async def add_catalog_ai(self):
        job_id, doc = uuid4().hex, document(source_id=self.retired)
        source = CatalogProduct.model_validate(doc['rows'][0]['source'])
        async with self.transaction() as db:
            supplier = await db.scalar(select(Supplier).where(Supplier.code == 'scope-fixture'))
            feed = SupplierFeed(supplier_id=supplier.id, code='products', name='Synthetic feed', feed_type='products')
            db.add(feed); await db.flush()
            db.add(SupplierProduct(id=self.retired, supplier_id=supplier.id, supplier_sku=source.code,
                name=source.name, source_feed_id=feed.id, attributes={'catalog': source.model_dump(mode='json')}))
            db.add(AiBatch(id=job_id, request_hash='e' * 64)); await db.flush()
            ctx = {'source_kind': 'catalog', 'shop': 'biketrek', 'supplier': 'scope-fixture', 'feed_key': 'products',
                'code': 'SYN-KEEP', 'product_ids': [source.id], 'facts': ai_content.facts([source])}
            db.add(AiJob(id=job_id, batch_id=job_id, kind='product', status='ready', revision=1, context=ctx))

    async def test_ai_catalog_ids_are_resolved_as_supplier_products_not_hub_products(self):
        await self.add_catalog_ai()
        preview = await self.preview()
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])

    async def test_catalog_source_supplier_namespace_must_match_actual_feed_owner(self):
        await self.add_catalog_ai()
        async with self.transaction() as db:
            other = Supplier(code='scope-unrelated', name='Synthetic other supplier')
            db.add(other); await db.flush()
            source = await db.get(SupplierProduct, self.retired)
            source.supplier_id = other.id
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        self.assertIn('catalog_source_namespace_changed',
            {b.get('reason') for b in self.report(preview)['blockers']})

    async def test_derived_manufacturer_proof_reuses_real_sku_policy_owner(self):
        from inventory_hub.sku_policy_models import SkuBrand, SkuBrandAlias, ProductSkuIdentity
        from inventory_hub.sku_policy_types import SkuIdentityInput
        from inventory_hub.services.sku_policy import identity_key
        item = SkuIdentityInput(brand='Synthetic Scope Maker', source_code='MODEL-SCOPE', code_kind='manufacturer',
            leaf_confirmed=True, source_ref='https://manufacturer.invalid/model-scope')
        async with self.transaction() as db:
            brand = SkuBrand(code='ZY', name=item.brand, kind='brand', source_ref=item.source_ref, created_by='fixture')
            db.add(brand); await db.flush()
            db.add(SkuBrandAlias(alias_key=item.brand.casefold(), alias=item.brand, brand_id=brand.id,
                source_ref=item.source_ref, created_by='fixture'))
            db.add(ProductSkuIdentity(product_id=self.keeper, brand_id=brand.id, source_code=item.source_code,
                source_code_fold=item.source_code.casefold(), code_kind='manufacturer', identity_key=identity_key(brand.id, item),
                source_ref=item.source_ref, brand_raw=item.brand, allocated_sku='SYN-KEEP', created_by='fixture'))
        doc = document()
        row = doc['rows'][0]
        for value in (row['source'], row['values']):
            value.update(brand=item.brand, manufacturer_code=item.source_code)
        row['source']['mapping_provenance'] = {'fields': {'manufacturer_code': {
            'manufacturer_code_confirmed': True, 'value': item.source_code, 'scope': 'feed', 'source': 'MPN', 'revision': 1}}}
        self.assertIsNone(row['values']['sku_identity'])
        await self.add_draft(doc)
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        record = next(r for r in self.report(preview)['records'] if r['table'] == 'product_import_drafts')
        self.assertTrue(any(p['namespace'] == 'resolved_import_hub_owner' and p['value'] == self.keeper for p in record['matched_paths']))

    async def test_ai_baseline_code_mismatch_is_unknown_even_when_both_codes_are_disjoint(self):
        draft_id, _, _ = await self.add_ai()
        async with self.transaction() as db:
            row = await db.get(ProductImportDraft, draft_id)
            doc = deepcopy(row.document); doc['rows'][0]['ai_baseline']['code'] = 'SYN-DIFFERENT-BASELINE'
            row.document = doc
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        self.assertIn('ai_draft_baseline_changed', {p['code'] for r in self.report(preview)['records'] for p in r['problems']})

    async def test_linked_ai_checks_entire_draft_and_baseline_before_context_exemption(self):
        _, job_id, _ = await self.add_ai()
        preview = await self.preview()
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        record = next(r for r in self.report(preview)['records'] if r['row_identity'].get('id') == job_id)
        self.assertEqual(record['classification'], 'context_only')
        self.assertEqual(len(record['linked_drafts']), 1)

    async def test_missing_ai_staging_id_never_inferred_from_equal_source_codes(self):
        await self.add_ai(staging=False)
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        ai = next(r for r in self.report(preview)['records'] if r['table'] == 'ai_content_jobs')
        self.assertEqual(ai['classification'], 'unknown')
        self.assertIn('ai_staging_kind_mismatch', {r['code'] for r in ai['problems']})

    async def test_missing_backlink_and_baseline_drift_fail_closed(self):
        draft_id, _, _ = await self.add_ai(linked=False)
        first = await self.preview()
        self.assertFalse(first['eligible'])
        self.assertIn('ai_draft_selection_changed', {p['code'] for r in self.report(first)['records'] for p in r['problems']})

    async def test_linked_job_disjoint_family_cannot_hide_other_draft_target(self):
        await self.add_ai(second_target=True)
        preview = await self.preview()
        ai = next(r for r in self.report(preview)['records'] if r['table'] == 'ai_content_jobs')
        self.assertEqual(ai['classification'], 'reference')
        self.assertTrue(any(p['value'] == 'SYN-OLD' for p in ai['matched_paths']))

    async def test_disjoint_draft_edit_after_preview_expires_apply_without_partial_alias(self):
        identifier, _ = await self.add_draft()
        preview = await self.preview()
        async with self.transaction() as db:
            await db.execute(text('UPDATE product_import_drafts SET revision=revision+1 WHERE id=:id'), {'id': identifier})
        with self.assertRaisesRegex(links.LinkError, 'consolidation_dependencies_changed'):
            await self.apply(preview)
        async with self.transaction() as db:
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM product_canonical_aliases')), 0)

    async def test_unknown_source_field_not_silently_dropped_by_catalog_model(self):
        doc = document(); doc['rows'][0]['source']['future_write_identity'] = {'product_id': self.retired}
        await self.add_draft(doc)
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        self.assertIn('unknown_schema_field', {p['code'] for r in self.report(preview)['records'] for p in r['problems']})

    async def test_malformed_linked_input_shape_returns_explicit_hold(self):
        job_id = uuid4().hex
        async with self.transaction() as db:
            db.add(AiBatch(id=job_id, request_hash='e' * 64)); await db.flush()
            db.add(AiJob(id=job_id, batch_id=job_id, kind='product', status='ready', revision=1,
                         context=['unknown future context shape']))
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        self.assertIn('consolidation_dependency_scope_unverified',
            {b['code'] for b in self.report(preview)['blockers']})

    async def test_invalid_journal_price_comparison_is_context_without_calling_it_settled(self):
        self.journal()
        with patch.object(catalog_import, 'import_result', side_effect=AssertionError('Recovery reader forbidden')):
            preview = await self.preview()
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        journal = self.report(preview)['catalog_journals'][0]
        self.assertFalse(journal['settled'])
        self.assertEqual(journal['classification'], 'context_only')
        self.assertIn('comparison_only_no_existing_parent_put', {p['reason'] for p in journal['context_paths']})

    async def test_uncertain_selected_journal_overrides_top_level_completed(self):
        path, value = self.journal(leaf='SYN-OLD', state='uncertain', attempted=True)
        value['result']['status'] = 'completed'; path.write_text(json.dumps(value))
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        self.assertIn('consolidation_catalog_journal_pending', {b['code'] for b in preview['dependencies']['blockers']})

    async def test_missing_journal_payload_is_unknown_even_without_string_match(self):
        path, value = self.journal()
        value['preview']['items'][0].pop('payload'); path.write_text(json.dumps(value))
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        self.assertIn('consolidation_dependency_scope_unverified', {b['code'] for b in preview['dependencies']['blockers']})

    async def test_saved_product_ids_dictionary_values_are_hub_claims(self):
        draft_id, doc = await self.add_draft(document(variant=False))
        path, value = self.journal()
        value['staging'] = {'draft_id': draft_id, 'hash': product_import.content_hash(doc),
                            'saved_product_ids': {'700': self.retired}}
        path.write_text(json.dumps(value))
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        self.assertTrue(any(p['path'] == '/staging/saved_product_ids/700' and p['value'] == self.retired
            for p in self.report(preview)['catalog_journals'][0]['matched_paths']))

    async def test_journal_sidecar_drift_after_preview_is_cas_failure(self):
        path, value = self.journal()
        preview = await self.preview()
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        # Even a disjoint sidecar must remain exactly the reviewed observation.
        catalog_import._write(path.with_suffix('.result.json'), value['result'])
        with self.assertRaisesRegex(links.LinkError, 'consolidation_dependencies_changed'):
            await self.apply(preview)

    async def test_late_new_unsent_file_does_not_authorize_stale_executor_identity(self):
        preview = await self.preview()
        original = service._dependencies
        saved = {}
        async def late_file(*args, **kwargs):
            result = await original(*args, **kwargs)
            _, value = self.journal(leaf='SYN-OLD', state='ready')
            saved['source'] = CatalogProduct.model_validate(value['sources'][0])
            return result
        with patch.object(service, '_dependencies', side_effect=late_file):
            self.assertEqual((await self.apply(preview))['status'], 'completed')
        from inventory_hub.services.catalog import CatalogError
        # A base preview is not a sending intent. The real final writer claim
        # runs under the same identity lock and rejects its now-retired SKU.
        async with self.transaction() as db:
            from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK
            await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
            with self.assertRaises(CatalogError) as error:
                await catalog_import.claim_product_prefix(db, [saved['source']])
            self.assertEqual(error.exception.code, 'local_identity_conflict')

    async def test_comparison_label_without_correct_source_parent_lineage_is_unknown(self):
        path, value = self.journal()
        value['preview']['price_lines'][0]['shop_matches'][0]['value'] = 'SYN-WRONG-PARENT'
        path.write_text(json.dumps(value))
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        record = self.report(preview)['catalog_journals'][0]
        self.assertIn('comparison_source_identity_changed', {p['code'] for p in record['problems']})

    async def test_unknown_json_table_reference_remains_blocked(self):
        async with self.transaction() as db:
            await db.execute(text('CREATE TABLE future_scope_writer(id bigint PRIMARY KEY, state text, intent jsonb)'))
            await db.execute(text("INSERT INTO future_scope_writer VALUES(1,'pending',jsonb_build_object('hub_product_id',CAST(:pid AS bigint)))"), {'pid': self.retired})
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        self.assertIn('future_scope_writer', {b.get('table') for b in preview['dependencies']['blockers']})

    async def test_known_table_read_cap_is_hard_hold(self):
        await self.add_draft()
        with patch.object(scan, 'MAX_TOTAL_BYTES', 1):
            preview = await self.preview()
        self.assertFalse(preview['eligible'])
        self.assertIn('consolidation_dependency_scope_unverified', {b['code'] for b in preview['dependencies']['blockers']})

    async def test_nowait_fence_rejects_active_json_writer_without_deadlock(self):
        identifier, _ = await self.add_draft()
        preview = await self.preview()
        async with self.sessions() as writer:
            await writer.execute(text('UPDATE product_import_drafts SET revision=revision+1 WHERE id=:id'), {'id': identifier})
            with self.assertRaisesRegex(links.LinkError, 'consolidation_dependency_writer_busy'):
                await asyncio.wait_for(self.apply(preview), 3)
            await writer.rollback()
        async with self.transaction() as db:
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM product_canonical_aliases')), 0)

    async def test_new_json_insert_after_final_scan_waits_until_commit(self):
        preview = await self.preview()
        scanned, finish = asyncio.Event(), asyncio.Event()
        original = service._dependencies
        async def observe(*args, **kwargs):
            result = await original(*args, **kwargs)
            scanned.set()
            await finish.wait()
            return result
        async def insert():
            return await self.add_draft(document(leaf='SYN-OLD', variant=False))
        with patch.object(service, '_dependencies', side_effect=observe):
            applying = asyncio.create_task(self.apply(preview))
            await asyncio.wait_for(scanned.wait(), 5)
            inserting = asyncio.create_task(insert())
            await asyncio.sleep(.05)
            self.assertFalse(inserting.done())
            finish.set()
            self.assertEqual((await asyncio.wait_for(applying, 10))['status'], 'completed')
            await asyncio.wait_for(inserting, 5)

    async def atomer(self, *, selected='SYN-UNRELATED', fidelity=False):
        campaign, family_key = 'f' * 64, 's:synthetic-scope'
        async with self.transaction() as db:
            await db.execute(text("INSERT INTO atomer_progress_sources(sha256,source_rows,identities) VALUES(:sha,1,'[]')"), {'sha': 'a' * 64})
            await db.execute(text("INSERT INTO atomer_review_exports(sha256,format,compressed) VALUES(:sha,'admin_csv',:content)"),
                             {'sha': 'b' * 64, 'content': b'synthetic-source'})
            run = await db.scalar(text('''INSERT INTO atomer_progress_runs(source_sha256,admin_export_sha256,checked_at,totals)
                VALUES(:sha,:admin,now(),'{}') RETURNING id'''), {'sha': 'a' * 64, 'admin': 'b' * 64})
            detail = {'code': selected, 'kind': 'standalone', 'state': 'needs_both', 'hub_id': None,
                      'ean': None, 'target_code': None, 'target_parent': None, 'product_id': None, 'variant_id': None}
            receipt = {}
            if fidelity:
                card = Product(sku='SYN-CONTENT-OTHER', name='Synthetic unrelated content unit'); db.add(card); await db.flush()
                xt = await db.scalar(select(Shop.id).where(Shop.code == 'xtrek'))
                bt_map = ShopProduct(product_id=card.id, shop_id=self.shop_id, external_code=selected,
                    is_variant=True, external_product_id='8800', external_variant_id='8801', parent_code='SYN-KEEP')
                xt_map = ShopProduct(product_id=card.id, shop_id=xt, external_code=selected,
                    is_variant=False, external_product_id='9900')
                db.add_all([bt_map, xt_map]); await db.flush()
                detail.update(hub_id=card.id, hub_sku=card.sku, target_code=selected, target_parent=selected, product_id='9900', state='linked')
                receipt = {'cards': {selected: {'state': 'verified', 'evidence': {'status': 'verified', 'card_id': card.id,
                    'xtrek': {'product_id': '9900', 'variant_id': None, 'code': selected, 'link_id': xt_map.id},
                    'biketrek': {'product_id': '8800', 'variant_id': '8801', 'code': selected, 'parent': 'SYN-KEEP', 'link_id': bt_map.id}}}},
                    'content_requested': True}
            await db.execute(text('''INSERT INTO atomer_progress_items(run_id,code,kind,state,family_key,detail)
                VALUES(:run,:code,'standalone','needs_both',:family,CAST(:detail AS jsonb))'''),
                {'run': run, 'code': selected, 'family': family_key, 'detail': json.dumps(detail)})
            await db.execute(text('''INSERT INTO atomer_catalog_campaigns
                (id,run_id,source_sha,admin_sha,shop_fingerprint,actor,selection,state,checked_run_id)
                VALUES(:id,:run,:source,:admin,:fp,'operator','{}','paused',:run)'''),
                {'id': campaign, 'run': run, 'source': 'a' * 64, 'admin': 'b' * 64, 'fp': 'd' * 64})
            guards = {'group': [0, 'open', 'default'], 'rows': {selected: [0, 'open', 'default']},
                      'scope': {selected: 'default'}, 'own': [], 'excluded': {}, 'blocked': {}}
            await db.execute(text('''INSERT INTO atomer_catalog_jobs(campaign,family_key,priority,leaf_count,stage,guards,receipt)
                VALUES(:c,:f,1,1,:stage,CAST(:guards AS jsonb),CAST(:receipt AS jsonb))'''),
                {'c': campaign, 'f': family_key, 'stage': 'fidelity' if fidelity else 'pending',
                 'guards': json.dumps(guards), 'receipt': json.dumps(receipt)})
        return campaign, family_key

    async def test_atomer_selected_dictionary_key_alone_is_real_reference(self):
        await self.atomer(selected='SYN-OLD')
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        atomer = next(r for r in self.report(preview)['records'] if r['table'] == 'atomer_catalog_jobs')
        self.assertIn('/guards/rows/SYN-OLD', {p['path'] for p in atomer['matched_paths']})

    async def test_actual_atomer_finish_keeps_complete_disjoint_scope_and_immutable_stock(self):
        campaign, family_key = await self.atomer()
        before = await self.stock()
        async with self.transaction() as db:
            await db.execute(text('''INSERT INTO atomer_review_work
                (kind,key,revision,status,scope,decision,note,instruction,result,updated_by)
                VALUES('item','SYN-UNRELATED',2,'working','default','undecided','','','','operator')'''))
            guards = {'group': [0, 'open', 'default'], 'rows': {'SYN-UNRELATED': [2, 'working', 'default']},
                'scope': {'SYN-UNRELATED': 'default'}, 'own': [['item', 'SYN-UNRELATED', 2]], 'excluded': {}, 'blocked': {}}
            await db.execute(text('''UPDATE atomer_catalog_jobs SET guards=CAST(:guards AS jsonb)
                WHERE campaign=:c AND family_key=:f'''), {'guards': json.dumps(guards), 'c': campaign, 'f': family_key})
            await db.execute(text('''UPDATE atomer_progress_items SET detail=detail ||
                '{"family_name":"SYN-OLD is a display label"}'::jsonb WHERE family_key=:f'''), {'f': family_key})
            job = dict((await db.execute(text('''SELECT * FROM atomer_catalog_jobs
                WHERE campaign=:c AND family_key=:f'''), {'c': campaign, 'f': family_key})).mappings().one())
            await atomer_engine._finish(db, {'id': campaign, 'actor': 'operator'}, job,
                {'SYN-UNRELATED': ('blocked', {'reason': 'Synthetic no outbound intent'})})
            current = (await db.execute(text("SELECT revision,status FROM atomer_review_work WHERE kind='item' AND key='SYN-UNRELATED'"))).one()
            self.assertEqual(tuple(current), (3, 'blocked'))
            saved = await db.scalar(text('SELECT guards FROM atomer_catalog_jobs WHERE campaign=:c AND family_key=:f'),
                                    {'c': campaign, 'f': family_key})
            self.assertEqual(saved, guards)
        preview = await self.preview()
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        atomer = next(r for r in self.report(preview)['records'] if r['table'] == 'atomer_catalog_jobs')
        self.assertEqual(atomer['classification'], 'context_only')
        self.assertIn('saved_and_current_atomer_review_scope_union', {p['reason'] for p in atomer['context_paths']})
        self.assertEqual((await self.apply(preview))['status'], 'completed')
        self.assertEqual(await self.stock(), before)

    async def test_atomer_drift_union_still_revalidates_affected_current_owner_at_apply(self):
        campaign, family_key = await self.atomer(fidelity=True)
        before = await self.stock()
        async with self.transaction() as db:
            await db.execute(text('''INSERT INTO atomer_review_work
                (kind,key,revision,status,scope,decision,note,instruction,result,updated_by)
                VALUES('item','SYN-UNRELATED',3,'blocked','default','undecided','','','','operator')'''))
        preview = await self.preview()
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        async with self.transaction() as db:
            await db.execute(text('''UPDATE atomer_progress_items SET detail=detail ||
                jsonb_build_object('hub_id',CAST(:owner AS bigint)) WHERE family_key=:f'''),
                {'owner': self.retired, 'f': family_key})
        with self.assertRaisesRegex(links.LinkError, 'consolidation_dependencies_changed'):
            await self.apply(preview)
        self.assertEqual(await self.stock(), before)
        fresh = await self.preview()
        self.assertFalse(fresh['eligible'])
        atomer = next(r for r in self.report(fresh)['records'] if r['table'] == 'atomer_catalog_jobs')
        self.assertEqual(atomer['classification'], 'reference')
        self.assertIn('hub_product_id', {p['namespace'] for p in atomer['matched_paths']})

    async def test_malformed_saved_atomer_guard_remains_unknown_after_current_review_changed(self):
        campaign, family_key = await self.atomer()
        async with self.transaction() as db:
            await db.execute(text('''INSERT INTO atomer_review_work
                (kind,key,revision,status,scope,decision,note,instruction,result,updated_by)
                VALUES('item','SYN-UNRELATED',3,'blocked','default','undecided','','','','operator')'''))
            await db.execute(text('''UPDATE atomer_catalog_jobs SET guards=jsonb_set(guards,
                '{rows,SYN-UNRELATED}','[true,"working","default"]'::jsonb) WHERE campaign=:c AND family_key=:f'''),
                {'c': campaign, 'f': family_key})
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        atomer = next(r for r in self.report(preview)['records'] if r['table'] == 'atomer_catalog_jobs')
        self.assertEqual(atomer['classification'], 'unknown')
        self.assertIn('atomer_review_guard_shape_unknown', {p['code'] for p in atomer['problems']})

    async def test_new_atomer_run_invalidates_preview_and_stale_campaign_scope(self):
        await self.atomer()
        before = await self.stock()
        preview = await self.preview()
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        async with self.transaction() as db:
            await db.execute(text('''INSERT INTO atomer_progress_runs(source_sha256,admin_export_sha256,checked_at,totals)
                VALUES(:sha,:admin,now(),'{}')'''), {'sha': 'a' * 64, 'admin': 'b' * 64})
        with self.assertRaisesRegex(links.LinkError, 'consolidation_dependencies_changed'):
            await self.apply(preview)
        self.assertEqual(await self.stock(), before)
        fresh = await self.preview()
        self.assertFalse(fresh['eligible'])
        atomer = next(r for r in self.report(fresh)['records'] if r['table'] == 'atomer_catalog_jobs')
        self.assertEqual(atomer['classification'], 'unknown')
        self.assertIn('atomer_current_run_changed', {p['code'] for p in atomer['problems']})

    async def test_atomer_source_header_writer_is_fenced_before_apply(self):
        await self.atomer()
        preview = await self.preview()
        async with self.sessions() as writer:
            await writer.execute(text("UPDATE atomer_progress_runs SET totals=totals || jsonb_build_object('changed',true)"))
            with self.assertRaisesRegex(links.LinkError, 'consolidation_dependency_writer_busy'):
                await asyncio.wait_for(self.apply(preview), 3)
            await writer.rollback()

    async def test_historical_other_shop_parent_requires_current_exact_leaf_owner(self):
        await self.atomer(fidelity=True)
        preview = await self.preview()
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        atomer = next(r for r in self.report(preview)['records'] if r['table'] == 'atomer_catalog_jobs')
        self.assertEqual(atomer['classification'], 'context_only')
        self.assertIn('historical_other_shop_container_exact_leaf_owner_checked', {p['reason'] for p in atomer['context_paths']})

    async def test_fidelity_parent_effect_does_not_get_historical_container_exemption(self):
        await self.atomer(fidelity=True)
        request = self.request()
        # Both selected product IDs remain unrelated to the Atomer source card,
        # but one current xTrek sale shares its native parent write target.
        xt_shop = None
        async with self.transaction() as db:
            xt_shop = await db.scalar(select(Shop.id).where(Shop.code == 'xtrek'))
            mapping = await db.get(ShopProduct, self.mapping_ids[1])
            mapping.shop_id = xt_shop
            mapping.is_variant = True
            mapping.parent_code = 'SYN-CONTENT-PARENT'
            mapping.external_product_id = '9900'
            mapping.external_variant_id = '9902'
        self.remote[1].update(is_variant=True, parent_code='SYN-CONTENT-PARENT', parent_id='9900', external_id='9902')
        target = request.mapping_actions[1].target
        target.shop_code = 'xtrek'
        target.is_variant, target.parent_code = True, 'SYN-CONTENT-PARENT'
        target.native_product_id, target.native_variant_id = 9900, 9902
        preview = await self.preview(request)
        self.assertFalse(preview['eligible'])
        atomer = next(r for r in self.report(preview)['records'] if r['table'] == 'atomer_catalog_jobs')
        self.assertIn('native_parent_write_id', {p['namespace'] for p in atomer['matched_paths']})

    async def test_existing_shop_ai_parent_update_blocks_all_affected_siblings(self):
        job_id = uuid4().hex
        async with self.transaction() as db:
            db.add(AiBatch(id=job_id, request_hash='e' * 64)); await db.flush()
            ctx = {'source_kind': 'shop', 'update_only': True, 'source_parameters_loaded': True,
                'shop': 'biketrek', 'code': 'SYN-KEEP', 'product_ids': [],
                'source_snapshot': {'code': 'SYN-KEEP', 'product_id': 9001, 'ean': None, 'variants': []},
                'facts': [{'id': 9001, 'code': 'SYN-KEEP', 'eans': []}]}
            db.add(AiJob(id=job_id, batch_id=job_id, kind='product', status='ready', revision=1, context=ctx))
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        ai = next(r for r in self.report(preview)['records'] if r['table'] == 'ai_content_jobs')
        self.assertIn('native_parent_write_id', {p['namespace'] for p in ai['matched_paths']})

    async def test_gtin_equivalent_upc_keeps_import_owner_reference(self):
        doc = document()
        doc['rows'][0]['values']['eans'] = [fixture.UPC]
        await self.add_draft(doc)
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        record = next(r for r in self.report(preview)['records'] if r['table'] == 'product_import_drafts')
        self.assertIn('barcode', {p['namespace'] for p in record['matched_paths']})
