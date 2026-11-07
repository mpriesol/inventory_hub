"""Bounded batch contracts, authenticated routes and actual source GET grouping."""
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from pydantic import ValidationError

from inventory_hub.shared_stock_sale_types import SharedStockSaleBatchPreviewRequest, SharedStockSaleBatchApplyRequest
from inventory_hub.services import shared_stock_sale_batches as batches, product_links as links
import test_shared_stock_sales as sale_fixture
import test_product_link_source as source_fixture


def group(index=0, leaves=2):
    item = sale_fixture.request(product_id=index+1)
    item['sales'] = [{'code': f'BATCH-{index}-{offset}', 'parent_code': f'PARENT-{index}', 'is_variant': True,
                     'native_product_id': 800+index, 'native_variant_id': 1000+index*50+offset,
                     'sale_to_stock_factor': 1} for offset in range(leaves)]
    return item


class BatchContractTests(unittest.TestCase):
    def test_fifty_cards_and_hundred_leaves_are_the_whole_batch_limit(self):
        valid = [group(index) for index in range(50)]
        SharedStockSaleBatchPreviewRequest(items=valid)
        for items in ([], valid+[group(50)], valid[:-1]+[group(49, 3)]):
            with self.subTest(count=len(items)), self.assertRaises(ValidationError):
                SharedStockSaleBatchPreviewRequest(items=items)

    def test_batch_cannot_mix_shops_cards_uuids_codes_or_native_targets(self):
        first, second = group(), group(1)
        for changes in ({'shop_code': 'xtrek'}, {'product_id': first['product_id']}, {'request_id': first['request_id']},
                        {'sales': [{**second['sales'][0], 'code': first['sales'][0]['code'].lower()}]},
                        {'sales': [{**second['sales'][0], 'native_variant_id': first['sales'][0]['native_variant_id']}]},
                        {'same_physical_item_confirmed': 1}, {'base_piece_unit_confirmed': 'true'}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                SharedStockSaleBatchPreviewRequest(items=[first, {**second, **changes}])
        item = {'id': str(uuid4()), 'preview_hash': 'a'*64, 'confirmed': True}
        SharedStockSaleBatchApplyRequest(shop_code='xtrek', items=[item])
        with self.assertRaises(ValidationError):
            SharedStockSaleBatchApplyRequest(shop_code='xtrek', items=[item, item])


class BatchRouteTests(unittest.TestCase):
    setUp = sale_fixture.SharedSaleRouteTests.setUp

    def requests(self):
        return [('preview', {'items': [group()]}), ('apply', {'shop_code': 'biketrek',
            'items': [{'id': str(uuid4()), 'preview_hash': 'a'*64, 'confirmed': True}]})]

    def test_both_static_routes_require_auth_before_database_or_source_and_hide_invalid_input(self):
        for name, body in self.requests():
            with patch.object(batches, name, AsyncMock()) as operation:
                before = len(self.db_calls)
                response = self.client.post('/shared-stock-sales/batch/'+name, json=body)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers['cache-control'], 'no-store')
                self.assertEqual(len(self.db_calls), before)
                operation.assert_not_awaited()
                invalid = self.client.post('/shared-stock-sales/batch/'+name,
                    json={**body, 'private': 'must-not-echo'}, headers=self.headers)
                self.assertEqual(invalid.status_code, 422)
                self.assertNotIn('must-not-echo', invalid.text)
                operation.assert_not_awaited()

    def test_partial_result_keeps_retry_after_and_never_routes_batch_as_a_uuid(self):
        for name, body in self.requests():
            with patch.object(batches, name, AsyncMock(return_value={'status': 'stopped', 'items': [], 'retry_after': 73})) as operation:
                response = self.client.post('/shared-stock-sales/batch/'+name, json=body, headers=self.headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.headers['retry-after'], '73')
                self.assertEqual(response.headers['cache-control'], 'no-store')
                operation.assert_awaited_once()

    def test_cookie_batch_writes_keep_same_origin_and_actor(self):
        @self.app.middleware('http')
        async def principal(request, call_next):
            request.state.hub_user = {'id': 7, 'role': 'operator'}
            request.state.hub_bearer = False
            return await call_next(request)
        for name, body in self.requests():
            with patch.object(batches, name, AsyncMock(return_value={'status': 'completed', 'items': []})) as operation:
                before = len(self.db_calls)
                response = self.client.post('/shared-stock-sales/batch/'+name, json=body)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(len(self.db_calls), before)
                operation.assert_not_awaited()
                response = self.client.post('/shared-stock-sales/batch/'+name, json=body,
                    headers={'Origin': 'http://testserver', 'X-Hub-Request': '1'})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(operation.await_args.kwargs['actor'], {'user_id': 7})


class BatchNativeReadTests(unittest.IsolatedAsyncioTestCase):
    setup_client = source_fixture.ProductLinkSourceTests.setup_client

    def source_rows(self):
        targets = [batches._native_target(sale) for index in range(50) for sale in group(index)['sales']]
        variants = [{'code': row['code'], 'variant_id': int(row['external_id']),
                     'product_code': row['parent_code'], 'product_id': int(row['parent_id']), 'ean': None}
                    for row in targets]
        parents = [{'code': f'PARENT-{index}', 'product_id': 800+index, 'variants_exists_yn': True,
                    'descriptions': [{'language': 'sk', 'title': f'Fixture parent {index}'}]} for index in range(50)]
        pages = [source_fixture.page('products', []), source_fixture.page('variants', variants),
                 source_fixture.page('products', parents)]
        return targets, pages

    async def test_hundred_exact_leaves_use_three_actual_gets_and_each_group_checks_its_own_scope(self):
        targets, pages = self.source_rows()
        self.setup_client(*pages)
        observed = await batches._read('biketrek', targets, [])
        self.assertEqual(len(self.session.calls), 3)
        self.assertEqual(observed.proof()['shared_native_requests'], 3)
        self.assertEqual(len(observed.observed['leaves']), 100)
        for offset in range(0, 100, 2):
            selected = links.verified_selection(observed.observed, targets[offset:offset+2])
            self.assertEqual(len(selected['leaves']), 2)
        self.assertTrue(all(call[1]['allow_redirects'] is False for call in self.session.calls))

    async def test_all_native_and_absence_reads_share_eight_request_budget_without_retry(self):
        targets, pages = self.source_rows()
        previous = [{'id': index+1, 'is_variant': index < 25, 'external_product_id': str(2000+index),
                     'external_variant_id': str(3000+index) if index < 25 else None} for index in range(50)]
        old_variant_parents = [{'product_id': 2000+index, 'variants_exists_yn': True,
                                'variants': [{'variant_id': 4000+index}]}
                               for index in range(25)]
        old_products = [{'product_id': 2000+index} for index in range(25, 50)]
        pages += [source_fixture.page('products', old_variant_parents[:12], number=1, pages=2, total=25),
                  source_fixture.page('products', old_variant_parents[12:], number=2, pages=2, total=25),
                  source_fixture.page('products', []),
                  source_fixture.page('products', old_products[:12], number=1, pages=2, total=25),
                  source_fixture.page('products', old_products[12:], number=2, pages=2, total=25),
                  source_fixture.page('products', [])]
        self.setup_client(*pages)
        with self.assertRaisesRegex(links.LinkError, 'source_request_limit'):
            await batches._read('biketrek', targets, previous)
        self.assertEqual(len(self.session.calls), 8, 'The ninth GET must never be sent')

    async def test_rate_limit_discards_partial_observation_without_followup_calls(self):
        targets, pages = self.source_rows()
        self.setup_client(pages[0], source_fixture.Response({}, 429, {'Retry-After': '83'}), pages[2])
        with self.assertRaises(links.LinkError) as stopped:
            await batches._read('biketrek', targets, [])
        self.assertEqual((stopped.exception.code, stopped.exception.retry_after), ('source_rate_limited', 83))
        self.assertEqual(len(self.session.calls), 2)
