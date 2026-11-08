"""PostgreSQL regressions for auditable, unresolved Atomer identity reports."""
import json
import unittest

from sqlalchemy import select, text

import test_product_dependency_scope_db as fixture
from test_product_dependency_atomer_legacy import CODE, kind_conflict
from inventory_hub.db_models import IdentifierType, ProductIdentifier, Shop
from inventory_hub.db_models_ext import ShopProduct
from inventory_hub.services import product_links as links, product_link_source as source


@unittest.skipUnless(fixture.fixture.TEST_URL, 'Dedicated localhost *_catalog_test database required')
class AtomerLegacyScopeDatabaseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ProductDependencyScopeDatabaseTests.asyncSetUp
    asyncTearDown = fixture.ProductDependencyScopeDatabaseTests.asyncTearDown
    transaction = fixture.ProductDependencyScopeDatabaseTests.transaction
    leaf = fixture.ProductDependencyScopeDatabaseTests.leaf
    discover = fixture.ProductDependencyScopeDatabaseTests.discover
    verify = fixture.ProductDependencyScopeDatabaseTests.verify
    absence = fixture.ProductDependencyScopeDatabaseTests.absence
    code_absence = fixture.ProductDependencyScopeDatabaseTests.code_absence
    preview = fixture.ProductDependencyScopeDatabaseTests.preview
    apply = fixture.ProductDependencyScopeDatabaseTests.apply
    stock = fixture.ProductDependencyScopeDatabaseTests.stock
    request = fixture.ProductDependencyScopeDatabaseTests.request
    report = fixture.ProductDependencyScopeDatabaseTests.report
    atomer = fixture.ProductDependencyScopeDatabaseTests.atomer

    async def change_detail(self, values):
        async with self.transaction() as db:
            await db.execute(text('UPDATE atomer_progress_items SET detail=detail || CAST(:value AS jsonb)'),
                             {'value': json.dumps(values)})

    async def audit(self):
        result = {}
        async with self.transaction() as db:
            for table in ('atomer_progress_items', 'atomer_progress_runs', 'atomer_catalog_campaigns',
                          'atomer_catalog_jobs', 'atomer_review_work', 'product_identifiers'):
                result[table] = await db.scalar(text(
                    f"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb) FROM {table} t"))
        return result

    def record(self, preview):
        return next(row for row in self.report(preview)['records'] if row['table'] == 'atomer_catalog_jobs')

    async def add_navigation(self, *, extra=None):
        async with self.transaction() as db:
            receipt = await db.scalar(text('SELECT receipt FROM atomer_catalog_jobs'))
            for namespace in ('xtrek', 'biketrek'):
                leaf = receipt['cards'][CODE]['evidence'][namespace]
                leaf.update(source._navigation({'url': 'https://shop.example/SYN-OLD',
                    'admin_url': 'https://admin.example/100'}, {}))
                leaf.update(extra or {})
            await db.execute(text('UPDATE atomer_catalog_jobs SET receipt=CAST(:value AS jsonb)'),
                             {'value': json.dumps(receipt)})

    async def add_kind_conflict(self):
        await self.atomer()
        raw, family, *_ = kind_conflict()
        detail = family[CODE]['detail']
        async with self.transaction() as db:
            await db.execute(text('''UPDATE atomer_progress_items SET kind=:kind,state=:state,
                family_key=:family,detail=CAST(:detail AS jsonb)'''),
                {'kind': detail['kind'], 'state': detail['state'], 'family': raw['family_key'],
                 'detail': json.dumps(detail)})
            await db.execute(text('UPDATE atomer_catalog_jobs SET family_key=:family'), {'family': raw['family_key']})

    async def test_invalid_disjoint_raw_identity_survives_apply_and_replay(self):
        await self.atomer()
        raw = '4006381333932'
        await self.change_detail({'ean': raw, 'target_ean': raw})
        async with self.transaction() as db:
            receipt = {'cards': {CODE: {'state': 'blocked', 'evidence': {
                'ean': raw, 'status': 'blocked', 'reason': 'atomer_source_ean_unverified',
                'card_id': None, 'xtrek': None, 'biketrek': None}}}}
            await db.execute(text("UPDATE atomer_catalog_jobs SET stage='blocked',receipt=CAST(:value AS jsonb)"),
                             {'value': json.dumps(receipt)})
        before, stock = await self.audit(), await self.stock()
        request = self.request()
        preview = await self.preview(request)
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        self.assertEqual(self.record(preview)['classification'], 'context_only')
        self.assertTrue(self.record(preview)['linked_input_manifest']['hash'])
        done = await self.apply(preview)
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(await self.preview(request), done)
        self.assertEqual(await self.apply(preview), done)
        self.assertEqual(await self.audit(), before)
        self.assertEqual(await self.stock(), stock)

    async def test_registered_unverified_raw_alias_is_a_real_dependency(self):
        await self.atomer()
        raw = 'LEGACY123R'
        await self.change_detail({'ean': raw})
        async with self.transaction() as db:
            db.add(ProductIdentifier(product_id=self.retired, identifier_type=IdentifierType.unverified_barcode,
                                     value=raw))
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        record = self.record(preview)
        self.assertEqual(record['classification'], 'reference')
        self.assertIn('atomer_raw_barcode', {m['namespace'] for m in record['matched_paths']})

    async def test_valid_compound_member_remains_a_real_dependency(self):
        await self.atomer()
        await self.change_detail({'ean': f'RAW123R/4006381333931;{fixture.fixture.EAN}'})
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        record = self.record(preview)
        self.assertEqual(record['classification'], 'reference')
        self.assertIn(fixture.fixture.EAN,
                      {m['value'] for m in record['matched_paths'] if m['namespace'] == 'barcode'})

    async def test_unsupported_compound_separator_retains_unknown_hold(self):
        await self.atomer()
        await self.change_detail({'ean': f'4006381333931|{fixture.fixture.EAN}'})
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        self.assertEqual(self.record(preview)['classification'], 'unknown')
        self.assertIn('atomer_barcode_tokens_unverified', {p['code'] for p in self.record(preview)['problems']})

    async def test_known_saved_navigation_preserves_exact_owner_and_parent_rules(self):
        await self.atomer(fidelity=True)
        await self.add_navigation()
        before, stock = await self.audit(), await self.stock()
        preview = await self.preview()
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        self.assertIn('known_shop_navigation_url', {p['reason'] for p in self.record(preview)['context_paths']})
        self.assertEqual((await self.apply(preview))['status'], 'completed')
        self.assertEqual(await self.audit(), before)
        self.assertEqual(await self.stock(), stock)

    async def test_unknown_native_receipt_fields_cannot_hide_new_scope(self):
        await self.atomer(fidelity=True)
        await self.add_navigation(extra={'future_native_ids': [9001]})
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        self.assertEqual(self.record(preview)['classification'], 'unknown')
        self.assertIn('unknown_schema_field', {p['code'] for p in self.record(preview)['problems']})

    async def test_real_report_kind_conflict_stays_unresolved_and_shop_namespaced(self):
        await self.add_kind_conflict()
        # xTrek product9001 is not the selected BIKETREK product9001.
        before, stock = await self.audit(), await self.stock()
        preview = await self.preview()
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        self.assertIn('reported_atomer_source_native_kind_conflict',
                      {p['reason'] for p in self.record(preview)['context_paths']})
        self.assertEqual((await self.apply(preview))['status'], 'completed')
        self.assertEqual(await self.audit(), before)
        self.assertEqual(await self.stock(), stock)
        self.assertEqual(before['atomer_progress_items'][0]['detail']['state'], 'review')
        self.assertEqual(before['atomer_catalog_jobs'][0]['stage'], 'pending')

    async def test_conflicting_kind_still_blocks_its_exact_native_leaf_and_parent(self):
        await self.add_kind_conflict()
        request = self.request()
        async with self.transaction() as db:
            shop_id = await db.scalar(select(Shop.id).where(Shop.code == 'xtrek'))
            mapping = await db.get(ShopProduct, self.mapping_ids[0])
            mapping.shop_id = shop_id
        request.mapping_actions[0].target.shop_code = 'xtrek'
        preview = await self.preview(request)
        self.assertFalse(preview['eligible'])
        record = self.record(preview)
        self.assertEqual(record['classification'], 'reference')
        self.assertTrue({'native_product', 'native_parent_write_id'} <= {m['namespace'] for m in record['matched_paths']})

    async def test_raw_identity_drift_invalidates_preview_before_any_alias_commit(self):
        await self.atomer()
        await self.change_detail({'ean': 'LEGACY123R'})
        preview = await self.preview()
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        before = await self.stock()
        await self.change_detail({'ean': fixture.fixture.EAN})
        with self.assertRaisesRegex(links.LinkError, 'consolidation_dependencies_changed'):
            await self.apply(preview)
        self.assertEqual(await self.stock(), before)
        fresh = await self.preview()
        self.assertEqual(self.record(fresh)['classification'], 'reference')

    async def test_known_navigation_drift_also_remains_in_full_row_hash(self):
        await self.atomer(fidelity=True)
        await self.add_navigation()
        preview = await self.preview()
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        await self.add_navigation(extra={'shop_url': 'https://shop.example/changed'})
        with self.assertRaisesRegex(links.LinkError, 'consolidation_dependencies_changed'):
            await self.apply(preview)


if __name__ == '__main__':
    unittest.main()
