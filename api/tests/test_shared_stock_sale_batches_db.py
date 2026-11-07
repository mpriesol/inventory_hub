"""Shared fresh reads with independent PostgreSQL journals and failed-apply fences."""
import asyncio
from copy import deepcopy
import os
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy import select, text

from inventory_hub.db_models import Product
from inventory_hub.db_models_ext import ShopProduct, ShopProductContent
from inventory_hub.shared_stock_sale_models import SharedStockSalePreview
from inventory_hub.shared_stock_sale_types import (SharedStockSalePreviewRequest, SharedStockSaleApplyRequest,
    SharedStockSaleBatchPreviewRequest, SharedStockSaleBatchApplyRequest)
from inventory_hub.services import product_links as links, shared_stock_sales as sales, shared_stock_sale_batches as batches
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK
import test_product_links_db as fixture


@unittest.skipUnless(os.environ.get('CATALOG_TEST_DATABASE_URL'),
                     'Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB')
class SharedStockSaleBatchDatabaseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ProductLinksDatabaseTests.asyncSetUp
    asyncTearDown = fixture.ProductLinksDatabaseTests.asyncTearDown
    transaction = fixture.ProductLinksDatabaseTests.transaction
    discover_remote = fixture.ProductLinksDatabaseTests.discover_remote
    verify_remote = fixture.ProductLinksDatabaseTests.verify_remote
    mappings = fixture.ProductLinksDatabaseTests.mappings
    snapshot = fixture.ProductLinksDatabaseTests.snapshot

    def requests(self):
        self.remote, requests = [], []
        for index, pid in enumerate(self.ids):
            remote = [fixture.leaf(f'BATCH-{index}-{offset}', 1000+index*10+offset,
                parent_code=f'FAMILY-{index}', parent_id=str(800+index)) for offset in (1, 2)]
            self.remote.extend(remote)
            requests.append(SharedStockSalePreviewRequest(request_id=uuid4(), shop_code='xtrek', product_id=pid,
                sales=[{'code': row['code'], 'parent_code': row['parent_code'], 'is_variant': True,
                    'native_product_id': int(row['parent_id']), 'native_variant_id': int(row['external_id']),
                    'sale_to_stock_factor': 1} for row in remote],
                identity_evidence='Reviewed identical complete fixture items and their unchanged stock unit.',
                same_physical_item_confirmed=True, base_piece_unit_confirmed=True))
        return requests

    async def preview_batch(self, requests):
        async with self.transaction() as db:
            return await batches.preview(db, SharedStockSaleBatchPreviewRequest(items=requests))

    async def apply_batch(self, preview, *, indices=None):
        chosen = range(len(preview['items'])) if indices is None else indices
        payload = SharedStockSaleBatchApplyRequest(shop_code='xtrek', items=[{
            'id': preview['items'][index]['data']['id'],
            'preview_hash': preview['items'][index]['data']['preview_hash'], 'confirmed': True} for index in chosen])
        async with self.transaction() as db:
            return await batches.apply(db, payload)

    async def apply_one(self, data):
        async with self.transaction() as db:
            return await sales.apply(db, data['id'], SharedStockSaleApplyRequest(
                preview_hash=data['preview_hash'], confirmed=True))

    async def states(self, preview):
        async with self.sessions() as db:
            return [(await db.get(SharedStockSalePreview, item['id'])).status for item in preview['items']]

    async def test_fifty_cards_share_one_read_per_phase_and_replay_keeps_individual_hashes(self):
        async with self.transaction() as db:
            additions = [Product(sku=f'BATCH-HUB-{index}', name=f'Fixture {index}') for index in range(47)]
            db.add_all(additions)
            await db.flush()
            self.ids.extend(row.id for row in additions)
        requests = self.requests()
        before = await self.snapshot()
        preview = await self.preview_batch(requests)
        self.assertEqual(preview['status'], 'completed')
        self.assertEqual(self.discover.await_count, 1)
        self.assertEqual(len(self.discover.await_args.args[1]), 100)
        self.assertEqual(self.discover.await_args.kwargs['max_requests'], 8)
        self.assertEqual(len({item['data']['preview_hash'] for item in preview['items']}), 50)
        for request, item in zip(requests, preview['items']):
            async with self.sessions() as db:
                row = await db.get(SharedStockSalePreview, item['id'])
                self.assertEqual(row.request_hash, sales.request_document(request)[1])
        completed = await self.apply_batch(preview)
        self.assertEqual(completed['status'], 'completed')
        self.assertEqual(self.discover.await_count, 2)
        self.verify.assert_not_awaited()
        self.assertNotEqual(preview['source_read']['id'], completed['source_read']['id'])
        self.assertEqual(len(await self.mappings()), 100)
        self.assertEqual(await self.snapshot(), before)
        replay = await self.apply_batch(preview)
        self.assertTrue(all(item['outcome'] == 'replayed' for item in replay['items']))
        self.assertEqual([item['data'] for item in completed['items']], [item['data'] for item in replay['items']])
        await self.preview_batch(requests)
        self.assertEqual(self.discover.await_count, 2)

    async def test_first_local_failure_preserves_completed_prefix_and_unattempted_tail(self):
        preview = await self.preview_batch(self.requests())
        before = await self.snapshot()
        async with self.transaction() as db:
            (await db.get(Product, self.ids[1])).is_active = False
        result = await self.apply_batch(preview)
        self.assertEqual([item['outcome'] for item in result['items']], ['completed', 'failed', 'not_attempted'])
        self.assertEqual(result['items'][1]['error']['code'], 'shared_stock_sale_piece_card_required')
        self.assertTrue(result['items'][1]['error']['known_unapplied'])
        self.assertNotIn('error', result['items'][2])
        self.assertEqual(await self.states(preview), ['completed', 'expired', 'prepared'])
        self.assertEqual({row.product_id for row in await self.mappings()}, {self.ids[0]})
        after = await self.snapshot()
        # Only the deliberately changed active flag differs from the snapshot.
        self.assertEqual(after['stock_balances'], before['stock_balances'])
        self.assertEqual(after['stock_movements'], before['stock_movements'])
        resumed = await self.apply_batch(preview, indices=[0, 2])
        self.assertEqual([item['outcome'] for item in resumed['items']], ['replayed', 'completed'])
        self.assertEqual(set(self.discover.await_args.args[1]), {'BATCH-2-1', 'BATCH-2-2'})

    async def test_new_duplicate_in_one_exact_scope_blocks_that_group_without_switching_native_id(self):
        preview = await self.preview_batch(self.requests())
        self.remote.append({**self.remote[2], 'external_id': '9999'})
        result = await self.apply_batch(preview)
        self.assertEqual([item['outcome'] for item in result['items']], ['completed', 'failed', 'not_attempted'])
        self.assertEqual(result['items'][1]['error']['code'], 'source_identity_changed')
        self.assertEqual(await self.states(preview), ['completed', 'expired', 'prepared'])
        self.assertEqual(len(await self.mappings()), 2)

    async def test_different_explicit_parent_scope_does_not_invalidate_a_selected_leaf(self):
        requests = self.requests()
        self.remote.append({**self.remote[0], 'external_id': '9999', 'parent_id': '9000', 'parent_code': 'OTHER'})
        result = await self.apply_batch(await self.preview_batch(requests))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(await self.mappings()), 6)

    async def test_preview_rejects_scoped_ambiguity_before_creating_that_journal(self):
        requests = self.requests()
        self.remote.append({**self.remote[2], 'external_id': '9999'})
        result = await self.preview_batch(requests)
        self.assertEqual([item['outcome'] for item in result['items']], ['prepared', 'failed', 'not_attempted'])
        async with self.sessions() as db:
            self.assertIsNone(await db.get(SharedStockSalePreview, str(requests[1].request_id)))
            self.assertIsNone(await db.get(SharedStockSalePreview, str(requests[2].request_id)))
        self.assertEqual(await self.mappings(), [])

    async def test_preview_racing_with_expired_individual_review_is_reported_as_replayed(self):
        requests = self.requests()
        read = batches._read
        async def raced_read(*args, **kwargs):
            observation = await read(*args, **kwargs)
            async with self.transaction() as db:
                raced = await sales.preview(db, requests[0])
            self.verify.side_effect = links.source.SourceError('source_rate_limited', 429, 61)
            with self.assertRaises(links.LinkError):
                await self.apply_one(raced)
            return observation
        with patch.object(batches, '_read', raced_read):
            result = await self.preview_batch(requests)
        self.assertEqual([item['outcome'] for item in result['items']], ['replayed', 'prepared', 'prepared'])
        self.assertEqual(result['items'][0]['data']['status'], 'expired')
        self.assertEqual(await self.mappings(), [])

    async def test_rate_limit_fences_only_first_pending_and_does_not_retry_or_expire_tail(self):
        preview = await self.preview_batch(self.requests())
        self.discover.side_effect = links.source.SourceError('source_rate_limited', 429, 73)
        result = await self.apply_batch(preview)
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(result['retry_after'], 73)
        self.assertEqual(result['batch_error']['code'], 'source_rate_limited')
        self.assertEqual([item['outcome'] for item in result['items']], ['failed', 'not_attempted', 'not_attempted'])
        self.assertTrue(result['items'][0]['error']['known_unapplied'])
        self.assertEqual(await self.states(preview), ['expired', 'prepared', 'prepared'])
        self.assertEqual(self.discover.await_count, 2)
        self.assertEqual(await self.mappings(), [])

    async def test_after_commit_response_failure_is_unknown_and_individual_get_recovers(self):
        preview = await self.preview_batch(self.requests())
        original = sales.apply
        async def lost_response(*args, **kwargs):
            await original(*args, **kwargs)
            raise RuntimeError('private fixture diagnostic must not be returned')
        with patch.object(sales, 'apply', lost_response):
            result = await self.apply_batch(preview)
        self.assertEqual([item['outcome'] for item in result['items']], ['outcome_unknown', 'not_attempted', 'not_attempted'])
        self.assertNotIn('known_unapplied', result['items'][0]['error'])
        self.assertNotIn('private fixture', str(result))
        async with self.sessions() as db:
            recovered = await sales.get(db, preview['items'][0]['id'])
        self.assertEqual(recovered['status'], 'completed')
        self.assertEqual(await self.states(preview), ['completed', 'prepared', 'prepared'])
        completed = await self.apply_batch(preview)
        self.assertEqual(completed['items'][0]['data'], recovered)
        self.assertEqual(len(await self.mappings()), 6)

    async def test_batch_and_individual_concurrent_apply_create_each_mapping_once(self):
        preview = await self.preview_batch(self.requests())
        batch_result, individual = await asyncio.gather(self.apply_batch(preview), self.apply_one(preview['items'][0]['data']))
        self.assertEqual(batch_result['status'], 'completed')
        self.assertEqual(batch_result['items'][0]['data'], individual)
        self.assertEqual(len(await self.mappings()), 6)

    async def test_failed_common_read_waits_for_individual_apply_and_recovers_its_commit(self):
        preview = await self.preview_batch(self.requests())
        reached, release = asyncio.Event(), asyncio.Event()
        inspect = sales._inspect
        async def hold(db, shop, product_id, selected, *, lock=False):
            value = await inspect(db, shop, product_id, selected, lock=lock)
            if lock and product_id == self.ids[0]:
                reached.set()
                await release.wait()
            return value
        with patch.object(sales, '_inspect', hold):
            individual = asyncio.create_task(self.apply_one(preview['items'][0]['data']))
            await asyncio.wait_for(reached.wait(), 10)
            self.discover.side_effect = links.source.SourceError('source_rate_limited', 429, 91)
            batch_task = asyncio.create_task(self.apply_batch(preview))
            await asyncio.sleep(.05)
            release.set()
            completed, stopped = await asyncio.wait_for(asyncio.gather(individual, batch_task), 10)
        self.assertEqual(stopped['status'], 'stopped')
        self.assertEqual(stopped['items'][0]['data'], completed)
        self.assertEqual(stopped['batch_error']['code'], 'source_rate_limited')
        self.assertEqual(await self.states(preview), ['completed', 'prepared', 'prepared'])
        self.assertEqual(len(await self.mappings()), 2)

    async def test_failed_batch_fence_prevents_delayed_batch_using_its_completed_source_read(self):
        preview = await self.preview_batch(self.requests())
        reached, release = asyncio.Event(), asyncio.Event()
        count = 0
        async def read(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                reached.set()
                await release.wait()
                return {'fingerprint': self.fingerprint, 'leaves': deepcopy(self.remote), 'requests': 1}
            raise links.source.SourceError('source_rate_limited', 429, 61)
        self.discover.side_effect = read
        delayed = asyncio.create_task(self.apply_batch(preview))
        await asyncio.wait_for(reached.wait(), 10)
        failed = await self.apply_batch(preview)
        release.set()
        late = await asyncio.wait_for(delayed, 10)
        self.assertTrue(failed['items'][0]['error']['known_unapplied'])
        self.assertTrue(late['items'][0]['error']['known_unapplied'])
        self.assertEqual(await self.states(preview), ['expired', 'prepared', 'prepared'])
        self.assertEqual(await self.mappings(), [])

    async def test_observation_age_is_rechecked_after_waiting_for_identity_lock(self):
        preview = await self.preview_batch(self.requests())
        clock, calls, reached = [1000.0], [0], asyncio.Event()
        check = batches._Observation.require_fresh
        def signal(observation, shop_code):
            check(observation, shop_code)
            calls[0] += 1
            if calls[0] == 3:
                reached.set()
        async with self.sessions() as holder:
            await holder.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
            with patch.object(batches, 'monotonic', lambda: clock[0]), patch.object(batches._Observation, 'require_fresh', signal):
                task = asyncio.create_task(self.apply_batch(preview))
                try:
                    await asyncio.wait_for(reached.wait(), 10)
                    clock[0] += 61
                finally:
                    await holder.commit()
                result = await asyncio.wait_for(task, 10)
        self.assertEqual(result['items'][0]['error']['code'], 'shared_stock_sale_batch_observation_expired')
        self.assertEqual(await self.states(preview), ['expired', 'prepared', 'prepared'])
        self.assertEqual(await self.mappings(), [])

    async def test_observation_expiring_after_flush_rolls_back_mappings_and_content_before_fence(self):
        preview = await self.preview_batch(self.requests())
        clock = [1000.0]
        cache = links._cache_metadata
        async def expired_after_write(*args, **kwargs):
            result = await cache(*args, **kwargs)
            clock[0] += 61
            return result
        with patch.object(batches, 'monotonic', lambda: clock[0]), patch.object(links, '_cache_metadata', expired_after_write):
            result = await self.apply_batch(preview)
        self.assertTrue(result['items'][0]['error']['known_unapplied'])
        self.assertEqual(result['items'][0]['error']['code'], 'shared_stock_sale_batch_observation_expired')
        self.assertEqual(await self.mappings(), [])
        async with self.sessions() as db:
            self.assertEqual(list(await db.scalars(select(ShopProductContent))), [])

    async def replacement_requests(self):
        requests = self.requests()
        async with self.transaction() as db:
            old = [ShopProduct(shop_id=self.shop_id, product_id=pid, external_code=f'OLD-{index}',
                external_product_id=str(700+index), external_variant_id='900' if index == 1 else None,
                parent_code='OLD-PARENT' if index == 1 else None, is_variant=index == 1, is_listed=True)
                for index, pid in enumerate(self.ids)]
            db.add_all(old)
            await db.flush()
            original_ids = [row.id for row in old]
        for index, request in enumerate(requests):
            body = request.model_dump(mode='json')
            body['sales'][0]['replace_deleted_mapping'] = {'mapping_id': original_ids[index],
                'native_product_id': 700+index, 'native_variant_id': 900 if index == 1 else None}
            requests[index] = SharedStockSalePreviewRequest(**body)
        return requests, original_ids

    async def test_native_replacement_absence_is_batched_by_kind_with_one_phase_budget(self):
        requests, original_ids = await self.replacement_requests()
        def proof(*args, **kwargs):
            self.assertTrue(kwargs['collect_conflicts'])
            return {'fingerprint': self.fingerprint, 'requests': 2, 'existing_mapping_ids': []}
        with patch.object(links.source, 'verify_deleted_products', AsyncMock(side_effect=proof)) as products, \
             patch.object(links.source, 'verify_deleted_variants', AsyncMock(side_effect=proof)) as variants:
            preview = await self.preview_batch(requests)
            self.assertEqual(preview['status'], 'completed')
            self.assertEqual(products.await_count, 1)
            self.assertEqual(variants.await_count, 1)
            self.assertEqual(sorted(len(call.args[1]) for call in products.await_args_list + variants.await_args_list), [1, 2])
            self.assertEqual({products.await_args.kwargs['max_requests'], variants.await_args.kwargs['max_requests']}, {5, 7})
            completed = await self.apply_batch(preview)
            self.assertEqual(completed['status'], 'completed')
            self.assertEqual(products.await_count, 2)
            self.assertEqual(variants.await_count, 2)
            self.assertEqual(completed['source_read']['shared_native_requests'], 5)
        self.assertEqual(len(await self.mappings()), 6)
        self.assertTrue(set(original_ids) <= {row.id for row in await self.mappings()})

    async def test_reappeared_original_blocks_only_its_group_after_completed_prefix(self):
        requests, original_ids = await self.replacement_requests()
        absent = {'fingerprint': self.fingerprint, 'requests': 2, 'existing_mapping_ids': []}
        with patch.object(links.source, 'verify_deleted_products', AsyncMock(return_value=absent)), \
             patch.object(links.source, 'verify_deleted_variants', AsyncMock(return_value=absent)) as variants:
            preview = await self.preview_batch(requests)
            variants.return_value = {**absent, 'existing_mapping_ids': [original_ids[1]]}
            result = await self.apply_batch(preview)
        self.assertEqual([item['outcome'] for item in result['items']], ['completed', 'failed', 'not_attempted'])
        self.assertEqual(result['items'][1]['error']['code'], 'shared_stock_sale_original_still_exists')
        self.assertTrue(result['items'][1]['error']['known_unapplied'])
        self.assertEqual(await self.states(preview), ['completed', 'expired', 'prepared'])
        mappings = {row.id: row for row in await self.mappings()}
        self.assertEqual(mappings[original_ids[0]].external_product_id, '800')
        self.assertEqual(mappings[original_ids[1]].external_product_id, '701')
        self.assertEqual(mappings[original_ids[2]].external_product_id, '702')
        self.assertEqual(len(mappings), 4)

    async def test_each_group_reloads_owners_after_previous_group_commits(self):
        preview = await self.preview_batch(self.requests())
        original = sales.apply
        async def competing_owner(db, identifier, payload, **kwargs):
            result = await original(db, identifier, payload, **kwargs)
            if identifier == preview['items'][0]['id']:
                async with self.transaction() as competing:
                    await competing.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
                    competing.add(ShopProduct(shop_id=self.shop_id, product_id=self.ids[0], external_code='BATCH-1-1',
                        external_product_id='801', external_variant_id='1011', parent_code='FAMILY-1',
                        is_variant=True, shared_stock=True, sale_to_stock_factor=1, sale_barcodes=[], is_listed=True))
            return result
        with patch.object(sales, 'apply', competing_owner):
            result = await self.apply_batch(preview)
        self.assertEqual([item['outcome'] for item in result['items']], ['completed', 'failed', 'not_attempted'])
        self.assertEqual(result['items'][1]['error']['code'], 'shared_stock_sale_ownership_conflict')
        self.assertEqual(await self.states(preview), ['completed', 'expired', 'prepared'])
        self.assertEqual({row.product_id for row in await self.mappings()}, {self.ids[0]})

    async def test_apply_rejects_over_hundred_frozen_leaves_before_source_without_fencing_valid_previews(self):
        requests = self.requests()
        self.remote = []
        preview = {'items': []}
        for index, request in enumerate(requests):
            body = request.model_dump(mode='json')
            body['sales'] = [{'code': f'LIMIT-{index}-{offset}', 'parent_code': f'FAMILY-{index}', 'is_variant': True,
                'native_product_id': 800+index, 'native_variant_id': 5000+index*50+offset, 'sale_to_stock_factor': 1}
                for offset in range(40)]
            self.remote.extend(fixture.leaf(sale['code'], sale['native_variant_id'], parent_code=sale['parent_code'],
                                           parent_id=str(sale['native_product_id'])) for sale in body['sales'])
            async with self.transaction() as db:
                data = await sales.preview(db, SharedStockSalePreviewRequest(**body))
            preview['items'].append({'id': data['id'], 'data': data})
        reads = self.discover.await_count
        result = await self.apply_batch(preview)
        self.assertEqual(result['failure_stage'], 'preflight')
        self.assertEqual(result['items'][2]['error']['code'], 'shared_stock_sale_batch_scope_changed')
        self.assertNotIn('known_unapplied', result['items'][2]['error'])
        self.assertEqual(self.discover.await_count, reads)
        self.assertEqual(await self.states(preview), ['prepared', 'prepared', 'prepared'])
        self.assertEqual(await self.mappings(), [])

    async def test_changed_connection_after_first_commit_stops_next_group(self):
        preview = await self.preview_batch(self.requests())
        self.current_fingerprint.side_effect = [self.fingerprint, 'b' * 64]
        result = await self.apply_batch(preview)
        self.assertEqual([item['outcome'] for item in result['items']], ['completed', 'failed', 'not_attempted'])
        self.assertEqual(result['items'][1]['error']['code'], 'source_connection_changed')
        self.assertEqual(await self.states(preview), ['completed', 'expired', 'prepared'])
        self.assertEqual(len(await self.mappings()), 2)
