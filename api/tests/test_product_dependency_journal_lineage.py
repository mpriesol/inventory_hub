"""Frozen/current journal identities, writer images and atomic apply regressions."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from catalog_fixtures import product
import test_product_consolidation_db as fixture
import test_product_dependency_scope_db as original
from inventory_hub import config_io
from inventory_hub.catalog_types import CatalogParameter, ShopImportOptions
from inventory_hub.db_models import Product
from inventory_hub.product_import_models import ProductImportDraft
from inventory_hub.services import catalog_import, product_import, product_links as links
from inventory_hub.services import product_dependency_journals as journals, product_dependency_scope as scope
from inventory_hub.services.catalog import CatalogError


OLD_EAN = '4006381333931'
CURRENT_EAN = '5901234123457'


def staged(*, saved=True, state='uncertain', old_owner=81, current_owner=82):
    old = original.document(leaf='SYN-JOURNAL-SOURCE', variant=False)
    old['rows'][0]['values'].update(code='SYN-JOURNAL-OLD', eans=[OLD_EAN])
    old['rows'][0]['hub_product_id'] = old_owner
    source = product_import.to_source(old['rows'][0])
    current = deepcopy(old)
    current['rows'][0]['values'].update(code='SYN-JOURNAL-CURRENT', eans=[CURRENT_EAN])
    current['rows'][0].update(hub_product_id=current_owner, canonical_code='SYN-JOURNAL-CURRENT')
    current['saved_hash'] = product_import.content_hash(current)
    draft = {'id': 'd' * 32, 'shop': 'biketrek', 'supplier': 'scope-fixture', 'document': current}
    item = {'code': source.shop_code, 'product_ids': [source.id], 'status': state,
            'payload': {'code': source.shop_code, 'ean': OLD_EAN}, 'product_write_attempted': True}
    job = {'preview': {'preview_id': 'e' * 32, 'shop': 'biketrek', 'supplier': 'scope-fixture',
                      'items': [deepcopy(item)]},
           'sources': [source.model_dump(mode='json')],
           'result': {'preview_id': 'e' * 32, 'shop': 'biketrek', 'status': 'completed',
                      'items': [deepcopy(item)]},
           'staging': {'draft_id': draft['id'], 'hash': product_import.content_hash(old)}}
    if saved:
        job['staging']['saved_product_ids'] = {str(source.id): old_owner}
    return job, draft


def inspect(job, draft, *, hubs=(), skus=(), barcodes=(), sales=()):
    selected = scope.Scope(set(hubs), set(), {s.casefold() for s in skus}, scope.barcode_keys(barcodes), set(),
                           set(sales), set(), set(), set())
    proof = scope.Proof(selected)
    settled = journals.parse(proof, job, 'biketrek', job['preview']['preview_id'],
                             {draft['id']: draft} if draft else {})
    return proof, settled


class JournalLineageTests(unittest.TestCase):
    def test_real_variant_writer_image_is_known_without_hiding_leaf_identity(self):
        source = product(group_code='JOURNAL', variant_relationship='explicit',
                         variant_attributes=[CatalogParameter(name='Size', value='M')])
        source.eans = [OLD_EAN]
        item = catalog_import.build_item([source], ShopImportOptions(), {}, True)
        self.assertEqual(item.status, 'ready')
        self.assertEqual(item.payload['variants'][0]['image'], {'url': source.images[0]})
        proof = scope.Proof(scope.Scope(set(), set(), set(), set(), set(),
            {('biketrek', source.shop_code.casefold())}, set(), set(), set()))
        self.assertEqual(scope.payload(proof, item.payload, 'biketrek', '/payload'), {source.shop_code})
        self.assertTrue(any(hit['path'] == '/payload/variants/0/code' for hit in proof.matches))

    def test_only_exact_variant_image_object_is_accepted(self):
        invalid = [None, [], 'image.jpg', {}, {'url': None}, {'url': ''}, {'url': '  '},
                   {'url': {'product_id': 2}}, {'url': 'https://example.invalid/x', 'product_id': 2}]
        for image in invalid:
            with self.subTest(image=image), self.assertRaises(scope.Unverified):
                proof = scope.Proof(scope.Scope(*[set() for _ in range(9)]))
                scope.payload(proof, {'code': 'SYN-PARENT', 'variants': [{'code': 'SYN-LEAF', 'image': image}]},
                              'biketrek', '/payload')
        with self.assertRaisesRegex(scope.Unverified, 'unknown_schema_field'):
            scope.payload(proof, {'code': 'SYN-PARENT', 'image': {'url': 'https://example.invalid/x'}},
                          'biketrek', '/payload')

    def test_hash_drift_preserves_both_owner_claims_and_uncertain_lifecycle(self):
        job, draft = staged()
        before = scope.digest([job, draft])
        proof, settled = inspect(job, draft, hubs=[81, 82])
        self.assertFalse(settled, 'Top-level completed and changed hash do not settle an uncertain item')
        self.assertEqual({m['value'] for m in proof.matches if m['namespace'] == 'hub_product_id'}, {81, 82})
        self.assertIn('changed_snapshot_retains_frozen_and_current_scope', {p['reason'] for p in proof.contexts})
        self.assertEqual(proof.linked_drafts, {draft['id']})
        self.assertEqual(before, scope.digest([job, draft]))

    def test_each_old_or_current_code_and_barcode_keeps_a_reference(self):
        cases = [({'skus': ['SYN-JOURNAL-OLD']}, '/sources/0'),
                 ({'skus': ['SYN-JOURNAL-CURRENT']}, '/linked_draft/document/rows/0'),
                 ({'barcodes': [OLD_EAN]}, '/preview/items/0/payload'),
                 ({'barcodes': [CURRENT_EAN]}, '/linked_draft/document/rows/0/values')]
        for selected, path in cases:
            with self.subTest(selected=selected):
                proof, settled = inspect(*staged(), **selected)
                self.assertFalse(settled)
                self.assertEqual(proof.result()['classification'], 'reference')
                self.assertTrue(any(m['path'].startswith(path) for m in proof.matches), proof.matches)

    def test_legacy_absent_or_empty_map_uses_complete_current_owner_scope(self):
        for empty_map in (False, True):
            for state in ('invalid', 'uncertain', 'created'):
                with self.subTest(empty_map=empty_map, state=state):
                    job, draft = staged(saved=False, state=state)
                    if empty_map:
                        job['staging']['saved_product_ids'] = {}
                    proof, settled = inspect(job, draft, hubs=[82])
                    self.assertEqual(settled, state == 'created')
                    self.assertEqual(proof.result()['classification'], 'reference')
                    self.assertIn('legacy_current_draft_owner_lookup', {p['reason'] for p in proof.contexts})

    def test_settled_scope_still_checks_owners_without_copying_current_content_hash(self):
        job, draft = staged(state='created')
        with patch.object(product_import, 'content_hash', side_effect=AssertionError('Unneeded settled digest')):
            proof, settled = inspect(job, draft, hubs=[81, 82])
            self.assertTrue(settled)
            self.assertEqual({m['value'] for m in proof.matches if m['namespace'] == 'hub_product_id'}, {81, 82})
            draft['document']['rows'][0]['hub_product_id'] = None
            with self.assertRaisesRegex(scope.Unverified, 'journal_saved_owner_changed'):
                inspect(job, draft)

    def test_missing_or_unrecognized_lineage_remains_unverified(self):
        mutations = [
            lambda j, d: j['staging'].update(draft_id='not-a-draft'),
            lambda j, d: j['staging'].update(hash='unknown'),
            lambda j, d: j['staging'].update(hash=True),
            lambda j, d: j['staging'].update(future_identity={'product_id': 2}),
            lambda j, d: j['staging'].update(saved_product_ids={'701': 81}),
            lambda j, d: j['staging'].update(saved_product_ids={'700': None}),
            lambda j, d: j['staging'].update(saved_product_ids={'700': True}),
            lambda j, d: d.update(supplier='different-supplier'),
            lambda j, d: d['document']['rows'][0].update(hub_product_id=None),
            lambda j, d: d['document']['rows'][0].update(id=701),
            lambda j, d: d['document']['rows'][0]['source'].update(feed_key='different-feed'),
            lambda j, d: d['document']['rows'][0]['values'].update(future_identity={'product_id': 2}),
        ]
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                job, draft = staged()
                mutation(job, draft)
                with self.assertRaises(scope.Unverified):
                    inspect(job, draft)
        job, _ = staged()
        with self.assertRaisesRegex(scope.Unverified, 'journal_draft_missing'):
            inspect(job, None)

    def test_unrelated_current_rows_are_also_in_the_proof(self):
        job, draft = staged()
        extra = original.document(source_id=701, leaf='SYN-EXTRA-SELECTED', variant=False)['rows'][0]
        draft['document']['rows'].append(extra)
        proof, _ = inspect(job, draft, skus=['SYN-EXTRA-SELECTED'])
        self.assertEqual(proof.result()['classification'], 'reference')
        self.assertTrue(any(m['path'].startswith('/linked_draft/document/rows/1') for m in proof.matches))


@unittest.skipUnless(fixture.TEST_URL, 'Dedicated localhost *_catalog_test database required')
class JournalLineageDatabaseTests(unittest.IsolatedAsyncioTestCase):
    transaction = original.ProductDependencyScopeDatabaseTests.transaction
    leaf = original.ProductDependencyScopeDatabaseTests.leaf
    discover = original.ProductDependencyScopeDatabaseTests.discover
    verify = original.ProductDependencyScopeDatabaseTests.verify
    absence = original.ProductDependencyScopeDatabaseTests.absence
    code_absence = original.ProductDependencyScopeDatabaseTests.code_absence
    preview = original.ProductDependencyScopeDatabaseTests.preview
    apply = original.ProductDependencyScopeDatabaseTests.apply
    request = original.ProductDependencyScopeDatabaseTests.request
    stock = original.ProductDependencyScopeDatabaseTests.stock
    report = original.ProductDependencyScopeDatabaseTests.report

    async def asyncSetUp(self):
        await original.ProductDependencyScopeDatabaseTests.asyncSetUp(self)
        async with self.transaction() as db:
            old = Product(sku='SYN-JOURNAL-OLD', name='Synthetic frozen journal owner')
            current = Product(sku='SYN-JOURNAL-CURRENT', name='Synthetic current journal owner')
            db.add_all([old, current]); await db.flush()
            self.old_owner, self.current_owner = old.id, current.id

    async def asyncTearDown(self):
        await original.ProductDependencyScopeDatabaseTests.asyncTearDown(self)

    async def save_journal(self, *, saved=True, selected=None):
        job, draft = staged(saved=saved, old_owner=self.old_owner, current_owner=self.current_owner)
        if selected == 'frozen':
            job['staging']['saved_product_ids']['700'] = self.retired
        elif selected == 'current':
            draft['document']['rows'][0]['hub_product_id'] = self.retired
        async with self.transaction() as db:
            db.add(ProductImportDraft(id=draft['id'], request_hash='a' * 64, revision=1,
                supplier=draft['supplier'], shop=draft['shop'], status='saved', document=draft['document']))
        path = config_io.shop_path('biketrek').parent / 'catalog-imports' / (job['preview']['preview_id'] + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(job))
        return path, job, draft

    async def test_disjoint_uncertain_stale_journal_can_be_preserved_but_cannot_publish(self):
        path, job, draft = await self.save_journal(saved=False)
        before = path.read_bytes()
        with patch.object(catalog_import, 'import_result', side_effect=AssertionError('No recovery reader')):
            preview = await self.preview()
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        record = self.report(preview)['catalog_journals'][0]
        self.assertFalse(record['settled'])
        self.assertEqual(record['classification'], 'context_only')
        async with self.transaction() as db:
            with self.assertRaises(CatalogError) as error:
                await product_import.assert_snapshot(db, job['staging'])
            self.assertEqual(error.exception.code, 'import_draft_changed')
        self.assertEqual((await self.apply(preview))['status'], 'completed')
        self.assertEqual(path.read_bytes(), before)
        async with self.transaction() as db:
            stored = await db.get(ProductImportDraft, draft['id'])
            self.assertEqual(stored.document, draft['document'])

    async def test_frozen_saved_owner_reference_blocks_even_with_current_disjoint_owner(self):
        await self.save_journal(selected='frozen')
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        self.assertIn('consolidation_catalog_journal_pending', {x['code'] for x in preview['dependencies']['blockers']})
        self.assertTrue(any(p['path'] == '/staging/saved_product_ids/700'
                            for p in self.report(preview)['catalog_journals'][0]['matched_paths']))

    async def test_current_owner_reference_blocks_even_with_frozen_disjoint_owner(self):
        await self.save_journal(selected='current')
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        self.assertIn('consolidation_catalog_journal_pending', {x['code'] for x in preview['dependencies']['blockers']})

    async def test_linked_draft_drift_after_review_rejects_apply(self):
        _, _, draft = await self.save_journal()
        preview = await self.preview()
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        async with self.transaction() as db:
            stored = await db.get(ProductImportDraft, draft['id'])
            changed = deepcopy(stored.document)
            changed['rows'][0]['values']['name'] = 'Changed synthetic draft content'
            stored.document = changed
        with self.assertRaisesRegex(links.LinkError, 'consolidation_dependencies_changed'):
            await self.apply(preview)

    async def test_frozen_journal_drift_after_review_rejects_apply(self):
        path, job, _ = await self.save_journal()
        preview = await self.preview()
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        job['preview']['items'][0]['payload']['descriptions'] = [{'language': 'sk', 'title': 'Changed frozen content'}]
        job['result']['items'][0]['payload'] = deepcopy(job['preview']['items'][0]['payload'])
        path.write_text(json.dumps(job))
        with self.assertRaisesRegex(links.LinkError, 'consolidation_dependencies_changed'):
            await self.apply(preview)
