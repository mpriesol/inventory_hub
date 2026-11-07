"""Immutable per-inspection reuse must preserve every logical dependency proof."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import event, select

import test_product_consolidation_db as fixture
import test_product_dependency_paging_db as paging
import test_product_dependency_scope_db as original
from inventory_hub.ai_content_models import AiBatch, AiJob
from inventory_hub.db_models import Product, Supplier
from inventory_hub.product_import_models import ProductImportDraft
from inventory_hub.sku_policy_models import SkuBrand, SkuBrandAlias
from inventory_hub.sku_policy_types import SkuIdentityInput
from inventory_hub.services import ai_content, product_import, product_links as links
from inventory_hub.services import product_dependency_inventory as inventory
from inventory_hub.services import product_dependency_scan as scan
from inventory_hub.services import product_dependency_scope as scope
from inventory_hub.services.product_dependency_reuse import ResolutionMemo


class ResolutionMemoTests(unittest.TestCase):
    def test_decoded_hit_cannot_mutate_future_proofs(self):
        memo = ResolutionMemo(1000)
        value = {'owners': [1], 'sku_proof': {'reasons': ['synthetic']}}
        memo.put(('namespace', 'identity'), value)
        value['owners'].append(2)
        hit = memo.get(('namespace', 'identity'))
        hit['sku_proof']['reasons'].append('changed')
        self.assertEqual(memo.get(('namespace', 'identity')),
            {'owners': [1], 'sku_proof': {'reasons': ['synthetic']}})

    def test_deterministic_eviction_and_raw_headroom_never_exceed_payload_limit(self):
        memo = ResolutionMemo(1000)
        budget = inventory.Budget(100, 100, 10000, None, reuse=memo)
        budget.retain(20)
        for i in range(8):
            memo.put(('n', str(i)), {'owners': [i]})
            budget.account_reuse()
        self.assertGreater(memo.evictions, 0)
        self.assertLessEqual(memo.bytes, 80)
        self.assertIsNone(memo.get(('n', '0')))
        self.assertEqual(memo.get(('n', '7')), {'owners': [7]})
        budget.retain(100)
        self.assertEqual(memo.bytes, 0)
        memo.put(('n', 'large'), {'owners': [1]})
        self.assertEqual(memo.bytes, 0)
        self.assertLessEqual(budget.peak_payload, budget.max_payload)


@unittest.skipUnless(fixture.TEST_URL, 'Dedicated localhost *_catalog_test database required')
class DependencyReuseDatabaseTests(unittest.IsolatedAsyncioTestCase):
    transaction = paging.DependencyPagingDatabaseTests.transaction
    leaf = paging.DependencyPagingDatabaseTests.leaf
    discover = paging.DependencyPagingDatabaseTests.discover
    verify = paging.DependencyPagingDatabaseTests.verify
    absence = paging.DependencyPagingDatabaseTests.absence
    code_absence = paging.DependencyPagingDatabaseTests.code_absence
    request = paging.DependencyPagingDatabaseTests.request
    current = paging.DependencyPagingDatabaseTests.current
    preview = paging.DependencyPagingDatabaseTests.preview
    apply = paging.DependencyPagingDatabaseTests.apply
    stock = fixture.ProductConsolidationDatabaseTests.stock
    asyncSetUp = paging.DependencyPagingDatabaseTests.asyncSetUp
    asyncTearDown = paging.DependencyPagingDatabaseTests.asyncTearDown

    async def repeated(self, count=5, leaves=10):
        identifiers = [uuid4().hex for _ in range(count)]
        draft_id, document = uuid4().hex, original.document()
        document['rows'] = []
        for i in range(leaves):
            group = i % count
            row = original.document(source_id=700 + i, leaf=f'SYN-REUSE-{i}',
                parent=f'SYN-REUSE-GROUP-{group}')['rows'][0]
            item = SkuIdentityInput(brand='Synthetic Reuse Maker', source_code=f'ARTICLE-{i}',
                code_kind='manufacturer', leaf_confirmed=True, source_ref='https://manufacturer.invalid/article')
            row['values']['sku_identity'] = item.model_dump(mode='json')
            row['ai_job_id'] = identifiers[group]
            row['ai_baseline'] = deepcopy(row['values'])
            document['rows'].append(row)
        async with self.transaction() as db:
            brand = SkuBrand(code='ZY', name='Synthetic Reuse Maker', kind='brand',
                source_ref='https://manufacturer.invalid', created_by='fixture')
            db.add(brand)
            await db.flush()
            db.add(SkuBrandAlias(alias_key=brand.name.casefold(), alias=brand.name,
                brand_id=brand.id, source_ref=brand.source_ref, created_by='fixture'))
            db.add(ProductImportDraft(id=draft_id, request_hash='d' * 64, revision=1,
                supplier='scope-fixture', shop='biketrek', status='draft', document=document))
            for identifier in identifiers:
                rows = [row for row in document['rows'] if row['ai_job_id'] == identifier]
                sources = [product_import.to_source(row) for row in rows]
                context = {'source_kind': 'import_draft', 'staging_id': draft_id, 'shop': 'biketrek',
                    'supplier': 'scope-fixture', 'code': rows[0]['group_key'],
                    'product_ids': [row['id'] for row in rows], 'facts': ai_content.facts(sources),
                    'source_digest': ai_content.source_digest(sources), 'options': document['options']}
                db.add(AiBatch(id=identifier, request_hash='e' * 64))
                await db.flush()
                db.add(AiJob(id=identifier, batch_id=identifier, kind='product', revision=1,
                    status='completed', context=context))
        return draft_id, document

    @staticmethod
    def logical(report):
        value = deepcopy(report)
        value['work'].pop('identity_reuse', None)
        value['work'].pop('peak_retained_payload_bytes', None)
        return value

    async def measured(self, cache_bytes):
        count = 0
        def observe(*args):
            nonlocal count
            count += 1
        event.listen(self.engine.sync_engine, 'before_cursor_execute', observe)
        try:
            with patch.object(scan, 'MAX_RESOLUTION_REUSE_BYTES', cache_bytes):
                return await self.current(), lambda: count
        finally:
            event.remove(self.engine.sync_engine, 'before_cursor_execute', observe)

    async def test_whole_draft_queries_reuse_without_changing_full_proofs_or_edges(self):
        await self.repeated()
        uncached, old_count = await self.measured(0)
        reused, new_count = await self.measured(4000000)
        self.assertEqual(self.logical(uncached), self.logical(reused))
        self.assertTrue(reused['complete'])
        self.assertFalse(reused['blockers'])
        self.assertEqual(reused['record_count'], 6)
        self.assertEqual(reused['linked_manifest']['edges'], 5)
        self.assertGreater(reused['work']['identity_reuse']['hits'], 0)
        self.assertLess(new_count(), old_count() / 2)

    async def test_eviction_and_no_cache_headroom_preserve_eligibility_and_manifest(self):
        await self.repeated()
        with patch.object(scan, 'MAX_RESOLUTION_REUSE_BYTES', 0):
            original_report = await self.current()
        with patch.object(scan, 'MAX_TOTAL_BYTES', original_report['work']['peak_retained_payload_bytes']):
            constrained = await self.current()
        for key in ('complete', 'proof_manifest', 'linked_manifest', 'records', 'blockers'):
            self.assertEqual(original_report[key], constrained[key])
        self.assertGreater(constrained['work']['identity_reuse']['evictions'], 0)
        self.assertLessEqual(constrained['work']['peak_retained_payload_bytes'],
            constrained['work']['retained_payload_limit_bytes'])

    async def test_fresh_scan_does_not_reuse_a_previous_negative_owner_result(self):
        _, document = await self.repeated()
        before = await self.current()
        async with self.transaction() as db:
            db.add(Product(sku=document['rows'][0]['values']['code'], name='Synthetic newly observed owner'))
        after = await self.current()
        self.assertTrue(before['complete'] and after['complete'])
        self.assertNotEqual(before['proof_manifest'], after['proof_manifest'])
        self.assertEqual(before['work']['identity_reuse']['misses'], after['work']['identity_reuse']['misses'])

    async def test_preview_apply_owner_drift_is_still_fenced_without_stock_changes(self):
        _, document = await self.repeated()
        stock = await self.stock()
        preview = await self.preview()
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        async with self.transaction() as db:
            db.add(Product(sku=document['rows'][0]['values']['code'], name='Synthetic concurrent foreign owner'))
        with self.assertRaises(links.LinkError) as caught:
            await self.apply(preview)
        self.assertEqual(caught.exception.code, 'consolidation_dependencies_changed')
        self.assertTrue(caught.exception.known_unapplied)
        self.assertEqual(stock, await self.stock())

    async def test_complete_query_and_supplier_namespace_keys_preserve_fresh_results(self):
        await self.repeated(count=1, leaves=1)
        selected = scope.Scope({self.keeper, self.retired}, set(), set(), set(), set(), set(), set(), set(), set())
        item = SkuIdentityInput(brand='Synthetic Reuse Maker', source_code='OTHER-ARTICLE',
            code_kind='manufacturer', leaf_confirmed=True,
            source_ref='https://manufacturer.invalid/other').model_dump(mode='json')
        base = {'code': 'SYN-NO-CURRENT-OWNER', 'supplier': 'scope-fixture', 'supplier_sku': 'SYN-SUPPLIER',
            'barcodes': [], 'expected': None, 'sku_identity': None, 'item_type': 'stock',
            'refresh_sku_from_feed': False, 'mapped_sku_identity': None}
        variations = [{}, {'code': 'SYN-KEEP'}, {'supplier': 'missing-namespace'},
            {'supplier_sku': 'SYN-OTHER-SUPPLIER'}, {'barcodes': [fixture.EAN]},
            {'expected': self.keeper}, {'sku_identity': item}, {'mapped_sku_identity': item},
            {'refresh_sku_from_feed': True}, {'item_type': 'service'}]
        memo_budget = inventory.Budget(16000000, 64000000, 256000000, None,
            reuse=ResolutionMemo(4000000))
        async with self.transaction() as db:
            supplier = await db.scalar(select(Supplier.id).where(Supplier.code == 'scope-fixture'))
            namespaces = {'scope-fixture': supplier}
            for pass_number in range(2):
                for i, change in enumerate(variations):
                    query = {**base, **change, 'path': f'/pass/{pass_number}/query/{i}'}
                    proof, fresh = scope.Proof(selected), scope.Proof(selected)
                    proof.queries, fresh.queries = [query], [deepcopy(query)]
                    await scan._resolve(db, [proof], {}, suppliers=namespaces, budget=memo_budget)
                    await scan._resolve(db, [fresh], {}, suppliers=namespaces,
                        budget=inventory.Budget(16000000, 64000000, 256000000, None))
                    self.assertEqual(proof.result(), fresh.result())
            self.assertEqual(memo_budget.reuse.misses, len(variations))
            self.assertEqual(memo_budget.reuse.hits, len(variations))
            proof = scope.Proof(selected)
            proof.queries = [{**base, 'path': '/new-namespace-snapshot'}]
            await scan._resolve(db, [proof], {}, suppliers={**namespaces, 'new-supplier': supplier + 1}, budget=memo_budget)
            self.assertEqual(memo_budget.reuse.misses, len(variations) + 1)
