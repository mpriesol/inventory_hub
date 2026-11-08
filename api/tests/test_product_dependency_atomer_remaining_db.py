"""Database proofs that new legacy spellings never erase pending identity effects."""
import json
import unittest

from sqlalchemy import select, text

import test_product_dependency_scope_db as fixture
import test_product_dependency_atomer_legacy_db as legacy
from test_product_dependency_atomer_remaining import CODE, OTHER_EAN, RAW_VALUES, report
from inventory_hub.db_models import IdentifierType, ProductIdentifier, Shop
from inventory_hub.db_models_ext import ShopProduct
from inventory_hub.services import product_links as links


@unittest.skipUnless(fixture.fixture.TEST_URL, 'Dedicated localhost *_catalog_test database required')
class AtomerRemainingScopeDatabaseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = legacy.AtomerLegacyScopeDatabaseTests.asyncSetUp
    asyncTearDown = legacy.AtomerLegacyScopeDatabaseTests.asyncTearDown
    transaction = legacy.AtomerLegacyScopeDatabaseTests.transaction
    leaf = legacy.AtomerLegacyScopeDatabaseTests.leaf
    discover = legacy.AtomerLegacyScopeDatabaseTests.discover
    verify = legacy.AtomerLegacyScopeDatabaseTests.verify
    absence = legacy.AtomerLegacyScopeDatabaseTests.absence
    code_absence = legacy.AtomerLegacyScopeDatabaseTests.code_absence
    preview = legacy.AtomerLegacyScopeDatabaseTests.preview
    apply = legacy.AtomerLegacyScopeDatabaseTests.apply
    stock = legacy.AtomerLegacyScopeDatabaseTests.stock
    request = legacy.AtomerLegacyScopeDatabaseTests.request
    report = legacy.AtomerLegacyScopeDatabaseTests.report
    atomer = legacy.AtomerLegacyScopeDatabaseTests.atomer
    audit = legacy.AtomerLegacyScopeDatabaseTests.audit
    record = legacy.AtomerLegacyScopeDatabaseTests.record
    change_detail = legacy.AtomerLegacyScopeDatabaseTests.change_detail

    async def add_report(self, detail):
        await self.atomer()
        async with self.transaction() as db:
            await db.execute(text('''UPDATE atomer_progress_items SET kind=:kind,state=:state,
                family_key=:family,detail=CAST(:detail AS jsonb)'''),
                {'kind': detail['kind'], 'state': detail['state'], 'family': detail['family_key'],
                 'detail': json.dumps(detail)})
            await db.execute(text('UPDATE atomer_catalog_jobs SET family_key=:family'), {'family': detail['family_key']})

    async def test_audited_raw_formats_survive_apply_replay_without_writing_atomer_or_identifiers(self):
        data = report('/'.join(RAW_VALUES))
        await self.add_report(data[1][CODE]['detail'])
        before, stock = await self.audit(), await self.stock()
        request = self.request()
        preview = await self.preview(request)
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        self.assertEqual(self.record(preview)['classification'], 'context_only')
        completed = await self.apply(preview)
        self.assertEqual(completed['status'], 'completed')
        self.assertEqual(await self.apply(preview), completed)
        self.assertEqual(await self.preview(request), completed)
        self.assertEqual(await self.audit(), before)
        self.assertEqual(await self.stock(), stock)

    async def test_exact_custom_raw_owner_is_a_dependency_without_promoting_a_gtin(self):
        raw = "'" + OTHER_EAN
        await self.add_report(report(raw)[1][CODE]['detail'])
        async with self.transaction() as db:
            db.add(ProductIdentifier(product_id=self.retired, identifier_type=IdentifierType.custom, value=raw))
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        record = self.record(preview)
        self.assertEqual(record['classification'], 'reference')
        self.assertIn(raw, {item['value'] for item in record['matched_paths'] if item['namespace'] == 'atomer_raw_barcode'})
        self.assertNotIn(OTHER_EAN, {item['value'] for item in record['matched_paths'] if item['namespace'] == 'barcode'})

    async def test_valid_member_after_legacy_tokens_and_exact_word_alias_still_block(self):
        await self.add_report(report(f'ABC-1234-567/Jacket 13 XS čierna;{fixture.fixture.EAN}')[1][CODE]['detail'])
        async with self.transaction() as db:
            db.add(ProductIdentifier(product_id=self.retired, identifier_type=IdentifierType.custom, value='13'))
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        matches = self.record(preview)['matched_paths']
        self.assertIn('13', {item['value'] for item in matches if item['namespace'] == 'atomer_raw_barcode'})
        self.assertIn(fixture.fixture.EAN, {item['value'] for item in matches if item['namespace'] == 'barcode'})

    async def test_raw_literal_change_invalidates_preview_even_without_classification_change(self):
        await self.add_report(report("'" + OTHER_EAN)[1][CODE]['detail'])
        preview = await self.preview()
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        stock = await self.stock()
        await self.change_detail({'ean': OTHER_EAN + '.'})
        with self.assertRaisesRegex(links.LinkError, 'consolidation_dependencies_changed'):
            await self.apply(preview)
        self.assertEqual(await self.stock(), stock)

    async def test_real_availability_conflict_is_preserved_through_disjoint_consolidation(self):
        data = report(OTHER_EAN, actual='Available', expected='Pending')
        await self.add_report(data[1][CODE]['detail'])
        before, stock = await self.audit(), await self.stock()
        preview = await self.preview()
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        self.assertIn('recorded_atomer_availability_conflict', {item['reason'] for item in self.record(preview)['context_paths']})
        self.assertEqual((await self.apply(preview))['status'], 'completed')
        self.assertEqual(await self.audit(), before)
        self.assertEqual(await self.stock(), stock)
        self.assertEqual(before['atomer_progress_items'][0]['detail']['state'], 'review')
        self.assertEqual(before['atomer_catalog_jobs'][0]['stage'], 'pending')

    async def test_availability_conflict_does_not_erase_exact_native_parent_dependency(self):
        await self.add_report(report(OTHER_EAN, actual='Available', expected='Pending')[1][CODE]['detail'])
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
        self.assertIn('native_parent_write_id', {item['namespace'] for item in record['matched_paths']})

    async def test_availability_label_drift_is_frozen_and_unknown_nested_scope_still_holds(self):
        await self.add_report(report(OTHER_EAN, actual='Available', expected='Pending')[1][CODE]['detail'])
        preview = await self.preview()
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        await self.change_detail({'availability_review': {'atomer': 'Other expected label', 'xtrek': 'Available'}})
        with self.assertRaisesRegex(links.LinkError, 'consolidation_dependencies_changed'):
            await self.apply(preview)
        await self.change_detail({'availability_review': {'atomer': 'Pending', 'xtrek': 'Available', 'future_native_id': 5}})
        fresh = await self.preview()
        self.assertFalse(fresh['eligible'])
        self.assertEqual(self.record(fresh)['classification'], 'unknown')
        self.assertIn('unknown_schema_field', {item['code'] for item in self.record(fresh)['problems']})


if __name__ == '__main__':
    unittest.main()
