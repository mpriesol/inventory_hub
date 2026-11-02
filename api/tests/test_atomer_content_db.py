"""Atomer content publication recovery against disposable local PostgreSQL.

Only the Upgates HTTP boundary is mocked. Source/family validation, native
mapping checks, selected-field payloads and durable publication fences run as
implemented. These tests never use production data or credentials.
"""
import asyncio
import copy
import json
import os
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from sqlalchemy import delete, select, text

from inventory_hub.atomer_content_types import AtomerContentItem
from inventory_hub.db_models import IdentifierType, ProductIdentifier
from inventory_hub.db_models_ext import ShopProduct, ShopProductContent
from inventory_hub.product_editor_models import ProductEditorPublication
from inventory_hub.product_editor_types import PublicationResolveRequest
from inventory_hub.services import atomer_content as service
from inventory_hub.services import atomer_content_source as transport
from inventory_hub.services import product_publication as publications
from inventory_hub.services.catalog import CatalogError
from inventory_hub.services.merchandising_write_guard import require_target_available
import test_product_editor_db as fixtures


SOURCE_HTML = (
    '<p><strong>Nutrition</strong></p><table><tbody><tr>'
    '<td>Energy</td><td>420</td></tr></tbody></table>'
)


@unittest.skipUnless(os.environ.get('CATALOG_TEST_DATABASE_URL'),
                     'Requires isolated localhost *_catalog_test database')
class AtomerContentDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.ProductEditorDatabaseTests.asyncSetUp(self)
        async with self.transaction() as db:
            rows = list(await db.scalars(select(ShopProduct).where(
                ShopProduct.shop_id == self.shops['xtrek'])))
            for row in rows:
                row.external_product_id = '501'
                row.external_variant_id = '601' if row.external_code == 'BIKE-2' else '602'
                row.is_listed = True
            db.add(ProductIdentifier(product_id=self.products['BIKE-10'],
                identifier_type=IdentifierType.ean, value='4006381333931', is_primary=True))
        async with self.engine.begin() as connection:
            raw = await connection.get_raw_connection()
            await raw.driver_connection.execute((self.sql_root / '005_ai_content.sql').read_text())

    asyncTearDown = fixtures.ProductEditorDatabaseTests.asyncTearDown
    transaction = fixtures.ProductEditorDatabaseTests.transaction

    def parent(self):
        return {
            'product_id': 501, 'code': 'REAL-FRAMES', 'ean': None,
            'manufacturer': 'Trek', 'variants_exists_yn': True, 'set_yn': False,
            'active_yn': True, 'stock': '5', 'availability': 'On request',
            'can_add_to_basket_yn': True,
            'descriptions': [
                {'language': 'sk', 'title': 'Frame family', 'short_description': 'Keep short text',
                 'long_description': '<p>Nutrition</p>', 'seo_title': 'Old title',
                 'seo_description': 'Old SEO description', 'url': 'https://shop.example.test/frame'},
                {'language': 'en', 'title': 'Original English', 'long_description': '<p>Keep English</p>'},
            ],
            'images': [{'url': 'https://images.example.test/original.jpg', 'main_yn': True}],
            'variants': [
                {'product_id': 501, 'product_code': 'REAL-FRAMES', 'variant_id': 601,
                 'code': 'BIKE-2', 'ean': '5901234123457', 'stock': '5',
                 'active_yn': True, 'availability': 'In stock', 'can_add_to_basket_yn': True},
                {'product_id': 501, 'product_code': 'REAL-FRAMES', 'variant_id': 602,
                 'code': 'BIKE-10', 'ean': '4006381333931', 'stock': '0',
                 'active_yn': True, 'availability': 'On request', 'can_add_to_basket_yn': True},
            ],
        }

    def snapshot(self):
        rows = []
        for position, (code, ean) in enumerate((('BIKE-2', '5901234123457'),
                                               ('BIKE-10', '4006381333931')), 1):
            rows.append({
                'code': code, 'parent_code': 'REAL-FRAMES', 'ean': ean, 'manufacturer': 'Trek',
                'long_description': SOURCE_HTML, 'seo_title': 'Complete source SEO title',
                'seo_description': 'Complete source SEO description', 'source_position': position,
                'raw_xml': f'<PRODUCT><PRODUCT_ID>{code}</PRODUCT_ID><PARENT_ID>REAL-FRAMES</PARENT_ID>'
                           '<COUNT_ON_STOCK>999</COUNT_ON_STOCK></PRODUCT>',
            })
        return {'sha256': 'b' * 64, 'size_bytes': 2500, 'total_rows': 2,
                'groups': {'REAL-FRAMES': {'source_code': 'REAL-FRAMES', 'rows': rows}}}

    def item(self, **changes):
        return AtomerContentItem(**{
            'request_id': uuid4(), 'hub_sku': 'BIKE-2', 'source_code': 'REAL-FRAMES',
            'fields': ['long_description', 'seo_title', 'seo_description'], **changes,
        })

    async def prepare(self, item=None, snapshot=None):
        async with self.sessions() as db:
            return await service.preview(db, item or self.item(), snapshot or self.snapshot())

    async def send(self, identifier):
        async with self.sessions() as db:
            return await publications.send(db, identifier)

    async def rollback_preview(self, original_id, request_id=None):
        async with self.sessions() as db:
            return await service.rollback_preview(db, original_id, request_id or str(uuid4()))

    async def protected_local_state(self):
        tables = ('products', 'product_identifiers', 'shop_products', 'product_editor_overrides',
                  'stock_balances', 'stock_movements', 'stock_tracking', 'reservations',
                  'fifo_states', 'fifo_layers', 'fifo_allocations', 'fifo_receipts')
        async with self.sessions() as db:
            return {table: list(await db.scalars(text(
                f'SELECT to_jsonb(item) FROM {table} AS item ORDER BY to_jsonb(item)::text')))
                    for table in tables}

    def network(self, stack, parent, *, after_write=None, uncertain=False):
        """Exercise real transport validation around an in-memory HTTP target."""
        stack.enter_context(patch.object(publications, 'fingerprint', return_value='a' * 64))
        client = SimpleNamespace(session=SimpleNamespace(close=Mock()))
        stack.enter_context(patch.object(transport.source, '_client', return_value=client))
        writes = []

        def request(_client, method, path, *, params=None, payload=None):
            self.assertEqual(path, 'products')
            if method == 'get':
                self.assertEqual(params['codes'], 'REAL-FRAMES')
                return {'current_page': 1, 'current_page_items': 1, 'number_of_items': 1,
                        'number_of_pages': 1, 'products': [copy.deepcopy(parent)]}
            self.assertEqual(method, 'put')
            writes.append(copy.deepcopy(payload))
            self.assertEqual(set(payload), {'products'})
            self.assertEqual(len(payload['products']), 1)
            product = payload['products'][0]
            self.assertEqual(set(product), {'code', 'descriptions'})
            self.assertEqual(product['code'], 'REAL-FRAMES')
            self.assertEqual(len(product['descriptions']), 1)
            change = product['descriptions'][0]
            self.assertEqual(change['language'], 'sk')
            self.assertLessEqual(set(change), {'language', *transport.FIELDS})
            target = next(row for row in parent['descriptions'] if row['language'] == 'sk')
            target.update(copy.deepcopy(change))
            if after_write:
                after_write(parent)
            if uncertain:
                # The remote side effect may have happened before a response was lost.
                raise transport.source.SourceError('synthetic_response_lost', uncertain=True)
            return {'products': [{'code': 'REAL-FRAMES', 'product_id': 501, 'updated_yn': True}]}

        stack.enter_context(patch.object(transport.source, '_request', side_effect=request))
        return writes

    async def test_uuid_replay_retains_source_proof_and_rejects_changed_input(self):
        item, snapshot = self.item(), self.snapshot()
        with ExitStack() as stack:
            writes = self.network(stack, self.parent())
            first = await self.prepare(item, snapshot)
            second = await self.prepare(item, snapshot)
            self.assertEqual(first['id'], str(item.request_id))
            self.assertEqual(second['id'], first['id'])
            self.assertEqual(second['state'], 'ready')
            changed = copy.deepcopy(snapshot)
            changed['sha256'] = 'c' * 64
            with self.assertRaises(publications.ERROR):
                await self.prepare(item, changed)
            self.assertEqual(writes, [])
        async with self.sessions() as db:
            records = list(await db.scalars(select(ProductEditorPublication)))
            self.assertEqual(len(records), 1)
            evidence = json.dumps(records[0].document, ensure_ascii=False)
        self.assertIn('b' * 64, evidence)
        self.assertIn('<COUNT_ON_STOCK>999</COUNT_ON_STOCK>', evidence)
        self.assertIn('atomer_content', evidence)

    async def test_success_replay_and_fresh_noop_do_not_change_local_stock_or_identity(self):
        parent = self.parent()
        before = await self.protected_local_state()
        with ExitStack() as stack:
            writes = self.network(stack, parent)
            ready = await self.prepare()
            result = await self.send(ready['id'])
            self.assertEqual(result['state'], 'completed')
            self.assertEqual((await self.send(ready['id']))['state'], 'completed')
            fresh = await self.prepare()
            self.assertEqual(fresh['state'], 'completed')
            self.assertTrue(fresh['no_op'])
            self.assertEqual((await self.send(fresh['id']))['state'], 'completed')
            self.assertEqual(len(writes), 1)
        self.assertEqual(before, await self.protected_local_state())

    async def test_concurrent_same_uuid_previews_create_one_durable_operation(self):
        item, snapshot = self.item(), self.snapshot()
        with ExitStack() as stack:
            writes = self.network(stack, self.parent())
            first, second = await asyncio.gather(self.prepare(item, snapshot), self.prepare(item, snapshot))
            self.assertEqual(first['id'], str(item.request_id))
            self.assertEqual(second['id'], first['id'])
            self.assertEqual(writes, [])
        async with self.sessions() as db:
            self.assertEqual(len(list(await db.scalars(select(ProductEditorPublication)))), 1)

    async def test_every_family_leaf_requires_an_explicit_native_shop_mapping(self):
        async with self.transaction() as db:
            await db.execute(delete(ShopProduct).where(ShopProduct.shop_id == self.shops['xtrek'],
                ShopProduct.product_id == self.products['BIKE-10']))
        before = await self.protected_local_state()
        with ExitStack() as stack:
            writes = self.network(stack, self.parent())
            with self.assertRaises(publications.ERROR):
                await self.prepare()
            self.assertEqual(writes, [])
        self.assertEqual(before, await self.protected_local_state())

    async def test_source_must_cover_the_complete_target_family(self):
        snapshot = self.snapshot()
        snapshot['groups']['REAL-FRAMES']['rows'].pop()
        with ExitStack() as stack:
            writes = self.network(stack, self.parent())
            with self.assertRaises(publications.ERROR):
                await self.prepare(snapshot=snapshot)
            self.assertEqual(writes, [])

    async def test_lost_html_structure_is_not_success_even_with_identical_text(self):
        def flatten(parent):
            parent['descriptions'][0]['long_description'] = 'NutritionEnergy420'
        with ExitStack() as stack:
            writes = self.network(stack, self.parent(), after_write=flatten)
            ready = await self.prepare()
            result = await self.send(ready['id'])
            self.assertEqual(result['state'], 'uncertain')
            self.assertTrue(result['error'])
            await self.send(ready['id'])
            self.assertEqual(len(writes), 1)

    async def test_untouched_language_is_checked_after_the_write(self):
        def alter_english(parent):
            parent['descriptions'][1]['title'] = 'Unexpected replacement'
        with ExitStack() as stack:
            writes = self.network(stack, self.parent(), after_write=alter_english)
            ready = await self.prepare()
            result = await self.send(ready['id'])
            self.assertEqual(result['state'], 'uncertain')
            self.assertTrue(result['protected_changes'])
            self.assertEqual(len(writes), 1)

    async def test_changed_operational_observation_is_flagged_without_stock_compensation(self):
        def sale_during_write(parent):
            parent['variants'][0]['stock'] = '4'
        before = await self.protected_local_state()
        with ExitStack() as stack:
            writes = self.network(stack, self.parent(), after_write=sale_during_write)
            ready = await self.prepare()
            result = await self.send(ready['id'])
            self.assertEqual(result['state'], 'uncertain')
            self.assertTrue(result['protected_changes'])
            self.assertEqual(len(writes), 1)
            self.assertNotIn('variants', writes[0]['products'][0])
        self.assertEqual(before, await self.protected_local_state())

    async def test_remote_edit_after_preview_stops_before_any_put(self):
        parent = self.parent()
        with ExitStack() as stack:
            writes = self.network(stack, parent)
            ready = await self.prepare()
            parent['descriptions'][0]['seo_title'] = 'A newer operator edit'
            with self.assertRaises(publications.ERROR):
                await self.send(ready['id'])
            self.assertEqual(writes, [])

    async def test_sibling_mapping_change_after_preview_stops_before_any_put(self):
        with ExitStack() as stack:
            writes = self.network(stack, self.parent())
            ready = await self.prepare()
            async with self.transaction() as db:
                mapping = await db.scalar(select(ShopProduct).where(
                    ShopProduct.shop_id == self.shops['xtrek'],
                    ShopProduct.product_id == self.products['BIKE-10']))
                mapping.external_variant_id = '999'
            with self.assertRaises(publications.ERROR):
                await self.send(ready['id'])
            self.assertEqual(writes, [])

    async def test_native_family_change_after_preview_stops_before_any_put(self):
        parent = self.parent()
        with ExitStack() as stack:
            writes = self.network(stack, parent)
            ready = await self.prepare()
            parent['variants'][1]['variant_id'] = 999
            with self.assertRaises(publications.ERROR):
                await self.send(ready['id'])
            self.assertEqual(writes, [])

    async def test_ambiguous_write_fences_other_parent_writers_until_explicit_resolution(self):
        parent = self.parent()
        with ExitStack() as stack:
            writes = self.network(stack, parent, uncertain=True)
            ready = await self.prepare()
            result = await self.send(ready['id'])
            self.assertEqual(result['state'], 'uncertain')
            await self.send(ready['id'])
            async with self.sessions() as db:
                with self.assertRaises(CatalogError):
                    await require_target_available(db, 'xtrek', 'REAL-FRAMES')
            with self.assertRaises(publications.ERROR):
                await self.prepare(self.item(hub_sku='BIKE-10'))
            async with self.sessions() as db:
                resolved = await publications.resolve(db, ready['id'], PublicationResolveRequest(
                    confirmed=True, original_request_settled=True,
                    note='Synthetic original HTTP request is confirmed settled.'))
            self.assertEqual(resolved['state'], 'completed')
            self.assertEqual(len(writes), 1)

    async def test_rollback_replays_and_restores_only_original_fields_preserving_fresh_other_content(self):
        parent = self.parent()
        original_values = {field: parent['descriptions'][0][field] for field in self.item().fields}
        before_local = await self.protected_local_state()
        with ExitStack() as stack:
            writes = self.network(stack, parent)
            original = await self.prepare()
            self.assertEqual((await self.send(original['id']))['state'], 'completed')
            # These newer edits are outside the original operation's field set.
            parent['descriptions'][0]['short_description'] = 'Newer approved short text'
            parent['descriptions'][1]['title'] = 'Newer English title'
            request_id = str(uuid4())
            rollback = await self.rollback_preview(original['id'], request_id)
            repeated = await self.rollback_preview(original['id'], request_id)
            self.assertEqual(rollback['id'], request_id)
            self.assertEqual(repeated['id'], request_id)
            self.assertEqual(rollback['rollback_of'], original['id'])
            self.assertEqual(rollback['after'], original_values)
            result = await self.send(rollback['id'])
            self.assertEqual(result['state'], 'completed')
            self.assertEqual((await self.send(rollback['id']))['state'], 'completed')
            self.assertEqual(len(writes), 2)
            self.assertEqual({field: parent['descriptions'][0][field] for field in original_values}, original_values)
            self.assertEqual(parent['descriptions'][0]['short_description'], 'Newer approved short text')
            self.assertEqual(parent['descriptions'][1]['title'], 'Newer English title')
        self.assertEqual(before_local, await self.protected_local_state())

    async def test_rollback_refuses_a_newer_edit_of_an_originally_selected_field(self):
        parent = self.parent()
        with ExitStack() as stack:
            writes = self.network(stack, parent)
            original = await self.prepare()
            await self.send(original['id'])
            parent['descriptions'][0]['seo_title'] = 'Newer selected title must survive'
            with self.assertRaisesRegex(publications.ERROR, 'rollback_remote_changed'):
                await self.rollback_preview(original['id'])
            self.assertEqual(len(writes), 1)
            self.assertEqual(parent['descriptions'][0]['seo_title'], 'Newer selected title must survive')

    async def test_rollback_refuses_changed_native_family(self):
        parent = self.parent()
        with ExitStack() as stack:
            writes = self.network(stack, parent)
            original = await self.prepare()
            await self.send(original['id'])
            parent['variants'][1]['variant_id'] = 999
            with self.assertRaises(publications.ERROR):
                await self.rollback_preview(original['id'])
            self.assertEqual(len(writes), 1)

    async def test_rollback_refuses_unsent_noop_and_rollback_operations(self):
        with ExitStack() as stack:
            writes = self.network(stack, self.parent())
            original = await self.prepare()
            with self.assertRaisesRegex(publications.ERROR, 'rollback_unavailable'):
                await self.rollback_preview(original['id'])
            await self.send(original['id'])
            no_op = await self.prepare()
            self.assertTrue(no_op['no_op'])
            with self.assertRaisesRegex(publications.ERROR, 'rollback_unavailable'):
                await self.rollback_preview(no_op['id'])
            reverse = await self.rollback_preview(original['id'])
            self.assertEqual((await self.send(reverse['id']))['state'], 'completed')
            with self.assertRaisesRegex(publications.ERROR, 'rollback_unavailable'):
                await self.rollback_preview(reverse['id'])
            self.assertEqual(len(writes), 2)

    async def test_preview_refuses_nonrestorable_empty_or_unknown_original_values(self):
        for empty in ('', None):
            with self.subTest(original=empty), ExitStack() as stack:
                parent = self.parent()
                if empty is None:
                    parent['descriptions'][0].pop('seo_description')
                else:
                    parent['descriptions'][0]['seo_description'] = empty
                writes = self.network(stack, parent)
                with self.assertRaises(publications.ERROR):
                    await self.prepare(self.item(fields=['seo_description']))
                self.assertEqual(writes, [])

    async def test_rollback_uuid_cannot_be_reassigned_to_another_completed_operation(self):
        with ExitStack() as stack:
            writes = self.network(stack, self.parent())
            original = await self.prepare()
            await self.send(original['id'])
            request_id = str(uuid4())
            await self.rollback_preview(original['id'], request_id)
            next_source = self.snapshot()
            next_source['sha256'] = 'c' * 64
            for row in next_source['groups']['REAL-FRAMES']['rows']:
                row['seo_title'] = 'Another approved source title'
            second = await self.prepare(self.item(fields=['seo_title']), next_source)
            self.assertEqual((await self.send(second['id']))['state'], 'completed')
            with self.assertRaisesRegex(publications.ERROR, 'request_changed'):
                await self.rollback_preview(second['id'], request_id)
            self.assertEqual(len(writes), 2)

    async def test_verified_receipt_preserves_link_metadata_and_extended_cached_sections(self):
        parent = self.parent()
        metadata = {
            f'variant:{leaf["variant_id"]}': {
                'code': leaf['code'], 'parent_code': parent['code'], 'is_variant': True,
                'external_id': str(leaf['variant_id']), 'parent_id': str(parent['product_id']),
                'name': 'Verified ' + leaf['code'],
                'shop_url': 'https://shop.example.test/frame#' + leaf['code'],
                'shop_admin_url': 'https://admin.example.test/product/' + str(leaf['variant_id']),
                'observed_at': '2026-11-02T12:00:00+00:00',
            } for leaf in parent['variants']
        }
        parameters = [{'descriptions': [{'language': 'sk', 'name': 'Material'}],
                       'values': [{'descriptions': [{'language': 'sk', 'value': 'Aluminium'}]}]}]
        files = [{'file_id': 77, 'url': 'https://files.example.test/manual.pdf'}]
        labels = [{'label_id': 88, 'name': 'Existing useful label'}]
        cached = copy.deepcopy(parent)
        cached.update(_hub_link_metadata=copy.deepcopy(metadata), parameters_new=copy.deepcopy(parameters),
                      files=copy.deepcopy(files), labels=copy.deepcopy(labels), stock='999')
        for leaf in cached['variants']:
            leaf['parameters_new'] = copy.deepcopy(parameters)
            leaf['files'] = copy.deepcopy(files)
            leaf['stock'] = '999'
        async with self.transaction() as db:
            content = await db.scalar(select(ShopProductContent).where(
                ShopProductContent.shop_id == self.shops['xtrek'],
                ShopProductContent.external_code == 'REAL-FRAMES'))
            content.data = cached
        with ExitStack() as stack:
            writes = self.network(stack, parent)
            ready = await self.prepare()
            self.assertEqual((await self.send(ready['id']))['state'], 'completed')
            self.assertEqual(len(writes), 1)
        async with self.sessions() as db:
            content = await db.scalar(select(ShopProductContent).where(
                ShopProductContent.shop_id == self.shops['xtrek'],
                ShopProductContent.external_code == 'REAL-FRAMES'))
            observed = content.data
        self.assertEqual(observed['_hub_link_metadata'], metadata)
        self.assertEqual(observed['parameters_new'], parameters)
        self.assertEqual(observed['files'], files)
        self.assertEqual(observed['labels'], labels)
        self.assertEqual(observed['stock'], parent['stock'])
        self.assertEqual(observed['descriptions'], parent['descriptions'])
        self.assertEqual(observed['descriptions'][0]['long_description'], SOURCE_HTML)
        for leaf in observed['variants']:
            with self.subTest(code=leaf['code']):
                remote = next(row for row in parent['variants'] if row['code'] == leaf['code'])
                self.assertEqual(leaf['variant_id'], remote['variant_id'])
                self.assertEqual(leaf['stock'], remote['stock'])
                self.assertEqual(leaf['parameters_new'], parameters)
                self.assertEqual(leaf['files'], files)
