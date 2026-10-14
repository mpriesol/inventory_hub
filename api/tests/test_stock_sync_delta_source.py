"""Non-idempotent stock increments are exact, bounded and attempted only once."""
from copy import deepcopy
from decimal import Decimal
import json
import unittest
from unittest.mock import Mock, patch

import requests

from inventory_hub.services import stock_sync_source as source
from inventory_hub.services.upgates import UpgatesClient, connection_fingerprint


BASE = 'https://delta-test.invalid/api/v2'
FINGERPRINT = connection_fingerprint(BASE, 'synthetic-reader')


def target(variant=False):
    return {'code': 'SKU-1', 'parent_code': 'UMBRELLA' if variant else 'SKU-1',
            'variant_code': 'SKU-1' if variant else None,
            'product_id': 41, 'variant_id': 71 if variant else None}


def desired(increment='2'):
    return {'stock_increment': increment, 'availability': 'SKLADOM', 'can_add_to_basket_yn': True}


def acknowledgement(variant=False):
    parent = {'code': 'UMBRELLA' if variant else 'SKU-1', 'product_id': 41,
              'updated_yn': True, 'messages': []}
    if variant:
        parent['variants'] = [{'code': 'SKU-1', 'variant_id': 71, 'updated_yn': True, 'messages': []}]
    return {'products': [parent]}


class StockSyncDeltaSourceTests(unittest.IsolatedAsyncioTestCase):
    def client(self, body, *, status=200, headers=None, raw=None):
        client = UpgatesClient(BASE, 'synthetic-reader', 'synthetic-key', verify_ssl=False)
        client.session = Mock(auth=('synthetic-reader', 'synthetic-key'))
        response = Mock(status_code=status, headers=headers or {})
        response.iter_content.return_value = [raw if raw is not None else json.dumps(body).encode()]
        client.session.put.return_value = response
        return client, response

    async def test_signed_delta_updates_exact_simple_or_variant_without_absolute_stock(self):
        for variant in (False, True):
            for increment in ('1', '-1', '9999999999', '-9999999999'):
                client, response = self.client(acknowledgement(variant))
                with self.subTest(variant=variant, increment=increment), \
                     patch.object(source.transport.UpgatesClient, 'from_shop', return_value=client):
                    result = await source.write_delta_once('biketrek', target(variant), desired(increment), FINGERPRINT)
                leaf = {'code': 'SKU-1', 'stock_increment': int(increment),
                        'availability': 'SKLADOM', 'can_add_to_basket_yn': True}
                product = {'code': 'UMBRELLA', 'variants': [leaf]} if variant else leaf
                self.assertEqual(result, {'acknowledged': True})
                client.session.put.assert_called_once_with(BASE + '/products', json={'products': [product]},
                    timeout=(5, 25), verify=True, allow_redirects=False, stream=True)
                client.session.get.assert_not_called()
                client.session.post.assert_not_called()
                response.close.assert_called_once()
                client.session.close.assert_called_once()

    async def test_zero_delta_is_metadata_only_and_does_not_touch_quantity(self):
        for variant in (False, True):
            client, _ = self.client(acknowledgement(variant))
            with patch.object(source.transport.UpgatesClient, 'from_shop', return_value=client):
                await source.write_delta_once('xtrek', target(variant), desired('0'), FINGERPRINT)
            product = client.session.put.call_args.kwargs['json']['products'][0]
            leaf = product['variants'][0] if variant else product
            self.assertEqual(leaf, {'code': 'SKU-1', 'availability': 'SKLADOM', 'can_add_to_basket_yn': True})
            self.assertNotIn('stock', product)
            self.assertNotIn('stock_increment', product)
            client.session.put.assert_called_once()
            client.session.get.assert_not_called()

    async def test_unknown_noncanonical_and_out_of_range_deltas_fail_before_network(self):
        invalid = (None, True, False, 1, -1, 1.0, Decimal('1'), '', '-0', '+1', '01', '-01',
                   '1.0', '-1.5', '1e3', '1,000', ' 1', '1 ', '1\n', 'NaN', 'Infinity',
                   '10000000000', '-10000000000', {}, [])
        for increment in invalid:
            with self.subTest(increment=increment), patch.object(source.transport, '_client') as client, \
                 self.assertRaises(source.SourceError) as raised:
                await source.write_delta_once('biketrek', target(), desired(increment), FINGERPRINT)
            self.assertEqual(raised.exception.code, 'stock_sync_invalid_payload')
            self.assertFalse(raised.exception.uncertain)
            client.assert_not_called()

    async def test_metadata_extra_fields_and_unfrozen_identity_fail_before_network(self):
        invalid_payloads = [None, {}, {**desired(), 'stock': '4'}, {**desired(), 'price': 3},
                            {key: value for key, value in desired().items() if key != 'stock_increment'}]
        invalid_payloads += [{**desired(), 'availability': value} for value in (None, '', 3, [], 'A' * 101, 'A\nB')]
        invalid_payloads += [{**desired(), 'can_add_to_basket_yn': value} for value in (None, False, 1, 'true')]
        for payload in invalid_payloads:
            with self.subTest(payload=payload), patch.object(source.transport, '_client') as client, \
                 self.assertRaises(source.SourceError):
                await source.write_delta_once('biketrek', target(), payload, FINGERPRINT)
            client.assert_not_called()
        for identity in ({key: value for key, value in target().items() if key != 'product_id'},
                         {**target(), 'product_id': True}, {**target(True), 'variant_id': None},
                         {**target(), 'stock_increment': 1}):
            with patch.object(source.transport, '_client') as client, self.assertRaises(source.SourceError):
                await source.write_delta_once('biketrek', identity, desired(), FINGERPRINT)
            client.assert_not_called()

    async def test_mismatched_or_partial_ack_is_uncertain_and_never_retried(self):
        good = acknowledgement(True)
        bodies = [{}, {'products': []}, {'products': good['products'] * 2}]
        for field, value in (('code', 'OTHER'), ('product_id', 42), ('updated_yn', False),
                             ('messages', [{'level': 'warning', 'message': 'not guaranteed'}]),
                             ('variants', [])):
            body = deepcopy(good)
            body['products'][0][field] = value
            bodies.append(body)
        for field, value in (('code', 'OTHER'), ('variant_id', 72), ('updated_yn', False),
                             ('messages', [{'level': 'error', 'message': 'not applied'}])):
            body = deepcopy(good)
            body['products'][0]['variants'][0][field] = value
            bodies.append(body)
        for body in bodies:
            client, response = self.client(body)
            with patch.object(source.transport.UpgatesClient, 'from_shop', return_value=client), \
                 self.assertRaises(source.SourceError) as raised:
                await source.write_delta_once('biketrek', target(True), desired('-2'), FINGERPRINT)
            self.assertTrue(raised.exception.uncertain)
            client.session.put.assert_called_once()
            client.session.get.assert_not_called()
            response.close.assert_called_once()
            client.session.close.assert_called_once()

    async def test_network_failure_invalid_json_and_server_failure_are_not_replayed(self):
        for failure in ('timeout', 'json', 'server'):
            client, response = self.client(acknowledgement(), status=500 if failure == 'server' else 200,
                                           raw=b'{broken' if failure == 'json' else None)
            if failure == 'timeout':
                client.session.put.side_effect = requests.ReadTimeout('synthetic timeout after sending')
            with self.subTest(failure=failure), \
                 patch.object(source.transport.UpgatesClient, 'from_shop', return_value=client), \
                 self.assertRaises(source.SourceError) as raised:
                await source.write_delta_once('biketrek', target(), desired('1'), FINGERPRINT)
            self.assertTrue(raised.exception.uncertain)
            client.session.put.assert_called_once()
            client.session.get.assert_not_called()
            client.session.close.assert_called_once()
            if failure != 'timeout':
                response.close.assert_called_once()

    async def test_rate_limit_returns_retry_hint_without_repeating_delta(self):
        client, response = self.client({}, status=429, headers={'Retry-After': '420'})
        with patch.object(source.transport.UpgatesClient, 'from_shop', return_value=client), \
             self.assertRaises(source.SourceError) as raised:
            await source.write_delta_once('biketrek', target(), desired('-1'), FINGERPRINT)
        self.assertEqual((raised.exception.status, raised.exception.retry_after), (429, 420))
        client.session.put.assert_called_once()
        client.session.get.assert_not_called()
        response.iter_content.assert_not_called()
        response.close.assert_called_once()
        client.session.close.assert_called_once()
