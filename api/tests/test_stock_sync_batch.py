"""Exact bounded read batches cannot certify incomplete or incoherent leaves."""
import copy
import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from inventory_hub.services import stock_sync_source as source
from inventory_hub.services.upgates import UpgatesClient, connection_fingerprint

BASE = 'https://batch-test.invalid/api/v2'
FINGERPRINT = connection_fingerprint(BASE, 'synthetic-reader')


def target(code, variant=False):
    return {'code': code, 'parent_code': 'UMBRELLA' if variant else code,
            'variant_code': code if variant else None}


def page(targets):
    variant = targets[0]['variant_code'] is not None
    rows = []
    for index, selected in enumerate(targets):
        row = {'code': selected['code'], 'product_id': 90 if variant else 100 + index, 'stock': 4,
               'availability': 'SKLADOM', 'can_add_to_basket_yn': True}
        if variant:
            row.update(product_code=selected['parent_code'], variant_id=200 + index)
        else:
            row.update(variants_exists_yn=False, variants=[], set_yn=False)
        rows.append(row)
    return {'current_page': 1, 'current_page_items': len(rows), 'number_of_items': len(rows),
            'number_of_pages': 1, 'variants' if variant else 'products': rows}


class StockSyncBatchTests(unittest.IsolatedAsyncioTestCase):
    def client(self, pages, *, status=200, headers=None):
        client = UpgatesClient(BASE, 'synthetic-reader', 'synthetic-key', verify_ssl=False)
        client.session = Mock(auth=('synthetic-reader', 'synthetic-key'))
        responses = []
        for body in pages:
            response = Mock(status_code=status, headers=headers or {})
            response.iter_content.return_value = [json.dumps(body).encode()]
            responses.append(response)
        client.session.get.side_effect = responses
        return client, responses

    async def read(self, targets, pages, **kwargs):
        client, responses = self.client(pages, **kwargs)
        with patch.object(source.transport.UpgatesClient, 'from_shop', return_value=client):
            result = await source.read_many('biketrek', targets, FINGERPRINT)
        client.session.close.assert_called_once()
        client.session.put.assert_not_called()
        client.session.post.assert_not_called()
        for response in responses:
            response.close.assert_called_once()
        return result, client

    async def test_twenty_unchanged_leaves_require_one_get_per_kind_and_preserve_exact_identity(self):
        for variant in (False, True):
            targets = [target(f'SKU-{index:02d}', variant) for index in range(20)]
            result, client = await self.read(targets, [page(targets)])
            client.session.get.assert_called_once_with(BASE + ('/products/variants' if variant else '/products/simple'),
                params={'variant_codes' if variant else 'codes': ';'.join(row['code'] for row in targets),
                        'page': 1, 'current_page_items': 100},
                timeout=(5, 25), verify=True, allow_redirects=False, stream=True)
            self.assertEqual(set(result), {row['code'] for row in targets})
            for selected in targets:
                result_row = result[selected['code']]
                self.assertEqual(result_row['observation']['stock'], '4')
                self.assertIsNotNone(result_row['observed_at'].tzinfo)
                self.assertGreater(result_row['observed_monotonic'], 0)
                for key in selected:
                    self.assertEqual(result_row['observation']['identity'][key], selected[key])

    async def test_mixed_simple_and_variant_filters_are_separate_requests(self):
        simple, variant = target('SIMPLE'), target('VARIANT', True)
        result, client = await self.read([simple, variant], [page([simple]), page([variant])])
        self.assertEqual(client.session.get.call_count, 2)
        self.assertEqual(set(result), {'SIMPLE', 'VARIANT'})
        self.assertEqual([call.kwargs['params'] for call in client.session.get.call_args_list], [
            {'codes': 'SIMPLE', 'page': 1, 'current_page_items': 100},
            {'variant_codes': 'VARIANT', 'page': 1, 'current_page_items': 100}])

    async def test_invalid_target_set_is_rejected_before_connection_or_request(self):
        for targets in ([], [target('A')] * 2, [target('A'), target('a', True)], [target('A;B')],
                        [target(str(index)) for index in range(101)]):
            with patch.object(source.transport, '_client') as client, self.assertRaises(source.SourceError):
                await source.read_many('biketrek', targets, FINGERPRINT)
            client.assert_not_called()

    def test_query_size_is_bounded_without_losing_or_reordering_target_codes(self):
        targets = [target(f'{index:02d}' + 'ž' * 98, True) for index in range(100)]
        groups = source.batch_groups(targets)
        self.assertGreater(len(groups), 1)
        self.assertEqual([row for group in groups for row in group['targets']], targets)
        for group in groups:
            encoded = urlencode({group['filter']: ';'.join(row['code'] for row in group['targets']),
                                 'page': 1, 'current_page_items': 100})
            self.assertLessEqual(len(encoded.encode('ascii')), source.MAX_BATCH_QUERY_BYTES)

    async def test_missing_duplicate_extra_or_paginated_results_do_not_fan_out(self):
        targets = [target('A', True), target('B', True)]
        good = page(targets)
        bodies = [dict(good, number_of_items=3), dict(good, number_of_pages=2),
                  dict(good, current_page=2), dict(good, current_page_items=True),
                  dict(good, variants=good['variants'][:1]),
                  dict(good, variants=[good['variants'][0], good['variants'][0]]),
                  dict(good, variants=[good['variants'][0], {**good['variants'][1], 'code': 'EXTRA'}]),
                  dict(good, current_page_items=1, number_of_items=1, variants=good['variants'][:1])]
        for body in bodies:
            client, _ = self.client([body])
            with patch.object(source.transport.UpgatesClient, 'from_shop', return_value=client), self.assertRaises(source.SourceError):
                await source.read_many('biketrek', targets, FINGERPRINT)
            client.session.get.assert_called_once()
            client.session.put.assert_not_called()

    async def test_malformed_leaf_never_certifies_an_unchanged_result(self):
        for patch_value in ({'product_id': True}, {'product_code': 'OTHER'}, {'variant_id': '9'},
                            {'stock': None}, {'stock': '1.25'}, {'can_add_to_basket_yn': 1},
                            {'availability': []}, {'messages': [{'level': 'error', 'message': 'private'}]}):
            targets = [target('A', True)]
            body = page(targets)
            body['variants'][0].update(patch_value)
            with self.subTest(patch_value=patch_value), self.assertRaises(source.SourceError):
                await self.read(targets, [body])

    async def test_nullable_orderability_is_unknown_and_never_matches_desired_true(self):
        from inventory_hub.services.stock_sync import matches
        targets = [target('A', True)]
        body = page(targets)
        body['variants'][0]['can_add_to_basket_yn'] = None
        result, _ = await self.read(targets, [body])
        observed = result['A']['observation']
        self.assertIsNone(observed['can_add_to_basket_yn'])
        self.assertFalse(matches(observed, {'stock': '4', 'availability': 'SKLADOM', 'can_add_to_basket_yn': True}))
        del body['variants'][0]['can_add_to_basket_yn']
        with self.assertRaises(source.SourceError):
            await self.read(targets, [body])

    async def test_duplicate_remote_ids_or_inconsistent_parent_identity_are_rejected(self):
        for variant, changes in ((False, {'product_id': 100}), (True, {'variant_id': 200}),
                                 (True, {'product_id': 91})):
            targets = [target('A', variant), target('B', variant)]
            body = page(targets)
            body['variants' if variant else 'products'][1].update(changes)
            with self.subTest(variant=variant, changes=changes), self.assertRaises(source.SourceError) as raised:
                await self.read(targets, [body])
            self.assertEqual(raised.exception.code, 'stock_publication_identity_changed')
        # Global consistency also spans HTTP groups, not just each envelope.
        targets = [target('SIMPLE'), target('VARIANT', True)]
        bodies = [page(targets[:1]), page(targets[1:])]
        bodies[1]['variants'][0]['product_id'] = 100
        with self.assertRaises(source.SourceError):
            await self.read(targets, bodies)

    async def test_rate_limit_stops_entire_batch_without_retrying_or_reading_second_kind(self):
        targets = [target('SIMPLE'), target('VARIANT', True)]
        client, responses = self.client([{}], status=429, headers={'Retry-After': '420'})
        with patch.object(source.transport.UpgatesClient, 'from_shop', return_value=client), self.assertRaises(source.SourceError) as raised:
            await source.read_many('biketrek', targets, FINGERPRINT)
        self.assertEqual((raised.exception.status, raised.exception.retry_after), (429, 420))
        self.assertFalse(raised.exception.uncertain)
        client.session.get.assert_called_once()
        client.session.put.assert_not_called()
        responses[0].iter_content.assert_not_called()
        responses[0].close.assert_called_once()
        client.session.close.assert_called_once()
