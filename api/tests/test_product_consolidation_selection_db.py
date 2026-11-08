"""Read-only repeatable-read operator selections against the complete schema."""
from decimal import Decimal
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

import test_product_consolidation_db as fixture
from inventory_hub.db_models import Product, ProductIdentifier, Shop
from inventory_hub.db_models_ext import ProductVariantAttribute, ShopProduct
from inventory_hub.product_editor_models import ProductEditorOverride
from inventory_hub.services import product_consolidation_selection as selection
from inventory_hub.services import product_links as links


@unittest.skipUnless(fixture.TEST_URL, 'Dedicated localhost *_catalog_test database required')
class SelectionDatabaseTests(unittest.IsolatedAsyncioTestCase):
    transaction = fixture.ProductConsolidationDatabaseTests.transaction
    leaf = fixture.ProductConsolidationDatabaseTests.leaf
    discover = fixture.ProductConsolidationDatabaseTests.discover
    verify = fixture.ProductConsolidationDatabaseTests.verify
    absence = fixture.ProductConsolidationDatabaseTests.absence
    code_absence = fixture.ProductConsolidationDatabaseTests.code_absence
    request = fixture.ProductConsolidationDatabaseTests.request
    preview = fixture.ProductConsolidationDatabaseTests.preview
    apply = fixture.ProductConsolidationDatabaseTests.apply
    stock = fixture.ProductConsolidationDatabaseTests.stock

    async def asyncSetUp(self):
        await fixture.ProductConsolidationDatabaseTests.asyncSetUp(self)

    async def asyncTearDown(self):
        await fixture.ProductConsolidationDatabaseTests.asyncTearDown(self)

    async def read(self, ids=None):
        async with self.transaction() as db:
            return await selection.inspect(db, ids or [self.keeper, self.retired])

    async def hashes(self):
        async with self.transaction() as db:
            return {table: await db.scalar(text(f'''SELECT md5(coalesce(
                string_agg(to_jsonb(t)::text,E'\\n' ORDER BY to_jsonb(t)::text),'')) FROM {table} t'''))
                for table in ('products', 'product_identifiers', 'product_variant_attributes', 'shop_products',
                    'shops', 'product_editor_overrides', 'product_consolidations', 'product_canonical_aliases',
                    'stock_balances', 'stock_movements', 'stock_tracking', 'fifo_layers', 'fifo_states')}

    async def test_complete_hidden_inactive_and_unsupported_shop_mappings_without_mutations(self):
        async with self.transaction() as db:
            inactive = Shop(code='synthetic-inactive', name='Inactive shop', platform='upgates', is_active=False)
            other = Shop(code='synthetic-other', name='Other platform', platform='other', is_active=True)
            db.add_all([inactive, other])
            await db.flush()
            hidden = await db.get(ShopProduct, self.mapping_ids[0])
            hidden.is_listed = False
            hidden.external_product_id = '9001'
            extra = [ShopProduct(shop_id=inactive.id, product_id=self.keeper, external_code='INACTIVE',
                        external_product_id='801', is_listed=False),
                     ShopProduct(shop_id=other.id, product_id=self.retired, external_code='OTHER',
                        external_product_id='802', is_listed=True)]
            db.add_all(extra)
            db.add(ProductVariantAttribute(product_id=self.keeper, attribute_name='size', attribute_value='M'))
            db.add(ProductEditorOverride(product_id=self.keeper, revision=1, data={
                'common': {'name': 'Reviewed display name'},
                'variant': {'attributes': [{'name': 'size', 'value': 'M'}], 'eans': [fixture.EAN]}}))
            await db.flush()
            extra_ids = [row.id for row in extra]
        before = await self.hashes()
        with patch.object(links.source, 'discover', AsyncMock(side_effect=AssertionError('native reads forbidden'))) as discover, \
             patch.object(links.source, 'verify', AsyncMock(side_effect=AssertionError('native reads forbidden'))) as verify:
            result = await self.read()
            discover.assert_not_awaited()
            verify.assert_not_awaited()
        self.assertEqual(await self.hashes(), before)
        self.assertEqual({row['id'] for row in result['mappings']}, {*self.mapping_ids, *extra_ids})
        hidden = next(row for row in result['mappings'] if row['id'] == self.mapping_ids[0])
        self.assertFalse(hidden['is_listed'])
        self.assertEqual(hidden['external_product_id'], '9001')
        self.assertTrue(hidden['shop_available'])
        unsupported = [row for row in result['mappings'] if row['id'] in extra_ids]
        self.assertTrue(all(row['shop_available'] is False for row in unsupported))
        self.assertEqual({row['mapping_id'] for row in result['blockers']}, set(extra_ids))
        self.assertEqual({row['code'] for row in result['blockers']}, {'consolidation_shop_unavailable'})
        keeper = next(row for row in result['products'] if row['id'] == self.keeper)
        self.assertEqual(keeper['name'], 'Reviewed display name')
        self.assertEqual(keeper['eans'], [fixture.EAN])
        self.assertEqual(keeper['manual_eans'], [fixture.EAN])
        self.assertEqual(keeper['attributes'], [{'name': 'size', 'value': 'M'}])
        self.assertIsNone(keeper['canonical_product_id'])
        self.assertEqual(set(hidden), {'id', 'product_id', 'shop_code', 'shop_name', 'shop_available',
            'external_code', 'parent_code', 'is_variant', 'external_product_id', 'external_variant_id',
            'is_listed', 'shared_stock', 'sale_to_stock_factor', 'push_pending'})

    async def test_one_snapshot_across_concurrent_identity_and_mapping_changes(self):
        original = links._facts
        async def change_then_read(db, product_ids):
            async with self.transaction() as other:
                card = await other.get(Product, self.keeper)
                card.name = 'Concurrent new name'
                mapping = await other.get(ShopProduct, self.mapping_ids[0])
                mapping.external_code = 'CONCURRENT-CODE'
                identifier = await other.scalar(select(ProductIdentifier).where(ProductIdentifier.product_id == self.keeper))
                identifier.value = fixture.OTHER_EAN
            return await original(db, product_ids)
        with patch.object(links, '_facts', side_effect=change_then_read):
            result = await self.read()
        keeper = next(row for row in result['products'] if row['id'] == self.keeper)
        self.assertEqual(keeper['name'], 'Synthetic identical retail unit')
        self.assertEqual(keeper['eans'], [fixture.EAN])
        self.assertEqual(result['mappings'][0]['external_code'], 'SYN-KEEP')
        fresh = await self.read()
        keeper = next(row for row in fresh['products'] if row['id'] == self.keeper)
        self.assertEqual(keeper['name'], 'Concurrent new name')
        self.assertEqual(keeper['eans'], [fixture.OTHER_EAN])
        self.assertEqual(fresh['mappings'][0]['external_code'], 'CONCURRENT-CODE')

    async def test_database_enforces_read_only_even_after_successful_selection(self):
        async with self.sessions() as db:
            await selection.inspect(db, [self.keeper, self.retired])
            self.assertEqual(await db.scalar(text('SHOW transaction_isolation')), 'repeatable read')
            self.assertEqual(await db.scalar(text('SHOW transaction_read_only')), 'on')
            with self.assertRaises(DBAPIError):
                await db.execute(text('UPDATE products SET name=name WHERE id=:id'), {'id': self.keeper})
            await db.rollback()

    async def test_missing_card_rejects_whole_selection_without_native_reads(self):
        before = await self.hashes()
        with self.assertRaisesRegex(links.LinkError, 'consolidation_product_missing'):
            await self.read([self.keeper, 9000000])
        self.assertEqual(await self.hashes(), before)
        links.source.discover.assert_not_awaited()
        links.source.verify.assert_not_awaited()

    async def test_more_than_fifty_mappings_is_rejected_instead_of_truncated(self):
        async with self.transaction() as db:
            for number in range(selection.MAX_MAPPINGS - 1):
                shop = Shop(code=f'limit-{number}', name=f'Synthetic shop {number}', platform='upgates')
                db.add(shop)
                await db.flush()
                db.add(ShopProduct(shop_id=shop.id, product_id=self.keeper,
                    external_code=f'MAP-{number}', external_product_id=str(80000 + number), is_listed=False))
        before = await self.hashes()
        with self.assertRaisesRegex(links.LinkError, 'consolidation_selection_mapping_limit'), \
             patch.object(links, '_facts', AsyncMock(side_effect=AssertionError('must stop before identity reads'))):
            await self.read()
        self.assertEqual(await self.hashes(), before)

    async def test_pending_shared_nonunit_and_nonstock_shapes_are_explicit_blockers(self):
        async with self.transaction() as db:
            mapping = await db.get(ShopProduct, self.mapping_ids[0])
            mapping.push_pending = True
            shared = await db.get(ShopProduct, self.mapping_ids[1])
            shared.shared_stock = True
            shared.external_product_id = '9002'
            shared.sale_to_stock_factor = Decimal(8)
            shared.sale_barcodes = [fixture.EAN]
            card = Product(sku='SYN-SERVICE', name='Synthetic service', item_type='service')
            db.add(card)
            await db.flush()
            service_id = card.id
            piece = Product(sku='SYN-PAIR', name='Synthetic pair', stock_unit='pair')
            db.add(piece)
            await db.flush()
            pair_id = piece.id
        result = await self.read([self.keeper, self.retired, service_id, pair_id])
        mapping_blocks = [b for b in result['blockers'] if b['code'] == 'consolidation_existing_mapping_mode_unsupported']
        self.assertEqual({row['mapping_id'] for row in mapping_blocks}, set(self.mapping_ids))
        shape_blocks = [b for b in result['blockers'] if b['code'] == 'consolidation_whole_piece_cards_required']
        self.assertEqual({row['product_id'] for row in shape_blocks}, {service_id, pair_id})
        self.assertEqual(next(row for row in result['mappings'] if row['id'] == self.mapping_ids[1])['sale_to_stock_factor'], '8.000')

    async def test_existing_alias_group_is_visible_at_original_ids_and_never_redirected(self):
        completed = await self.apply(await self.preview())
        before = await self.hashes()
        result = await self.read()
        self.assertEqual(await self.hashes(), before)
        self.assertEqual({row['id'] for row in result['products']}, {self.keeper, self.retired})
        self.assertTrue(all(row['canonical_product_id'] == self.keeper for row in result['products']))
        self.assertEqual({row['product_id'] for row in result['blockers']
            if row['code'] == 'consolidation_existing_alias_group_unsupported'}, {self.keeper, self.retired})
        self.assertEqual({row['id'] for row in result['mappings']},
            {row['id'] for row in completed['result']['current_mappings']})
        self.assertFalse(set(self.mapping_ids) & {row['id'] for row in result['mappings']})
        retired = next(row for row in result['products'] if row['id'] == self.retired)
        self.assertEqual(retired['sku'], 'SYN-OLD')
        self.assertFalse(retired['is_active'])
        self.assertEqual(retired['eans'], [fixture.UPC])
