"""A diagnostic must not become another stock writer or leak remote content."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import FastAPI
from pydantic import SecretStr
import requests

from inventory_hub.database import get_session
from inventory_hub.routers import shops
from inventory_hub.services import upgates_capacity as service
from inventory_hub.settings import settings


BODY = {'services': {
    'products': {'service': 'Products', 'url': 'https://secret.invalid/products', 'privilege': 'all'},
    'orders': {'service': 'Orders', 'url': 'https://secret.invalid/orders', 'privilege': 'read'},
    'OrderStatuses': {'service': 'OrderStatuses', 'url': 'https://secret.invalid/statuses', 'privilege': 'deny'},
}, 'documentation_link': 'https://secret.invalid/docs'}
HEADERS = {'X-Rate-Limit-Hour': '100', 'X-Rate-Limit-Day': '1500',
           'X-Rate-Limit-Hour-Remaining': '0', 'X-Rate-Limit-Day-Remaining': '1372',
           'X-Rate-Limit-Total-Remaining': '1372', 'X-Private': 'private-header'}
TOKEN = 'synthetic-capacity-access-token-123456'


class CapacityProbeTests(unittest.TestCase):
    def probe(self, *, body=None, raw=None, status=200, headers=None, failure=None, base=None):
        response = Mock(status_code=status, headers=HEADERS if headers is None else headers)
        response.iter_content.return_value = iter([raw if raw is not None else json.dumps(BODY if body is None else body).encode()])
        session = Mock(auth=('private-login', 'private-key'))
        session.get.return_value = response
        if failure:
            session.get.side_effect = failure
        client = SimpleNamespace(base_url=base or 'https://private-target.invalid/api/v2', session=session)
        with patch.object(service.UpgatesClient, 'from_shop', return_value=client):
            report = service.probe('biketrek')
        self.assertEqual(session.method_calls[-1][0], 'close')
        return report, session, response

    def test_one_read_only_request_uses_safe_transport_and_returns_allowlisted_data(self):
        report, session, response = self.probe()
        session.get.assert_called_once_with('https://private-target.invalid/api/v2/status',
            timeout=(5, 10), verify=True, allow_redirects=False, stream=True)
        self.assertEqual([call[0] for call in session.method_calls], ['get', 'close'])
        response.close.assert_called_once()
        self.assertTrue(report['probe']['ok'])
        self.assertEqual(report['probe']['requests_attempted'], 1)
        self.assertIsInstance(report['probe']['latency_ms'], int)
        self.assertEqual(len(report['probe']['target_fingerprint']), 64)
        self.assertEqual(report['probe']['permissions'], {'products': 'all', 'orders': 'read', 'order_statuses': 'deny'})
        self.assertFalse(report['probe']['write_tested'])
        self.assertEqual(report['quota']['hour_remaining'], 0)
        self.assertEqual(report['quota']['total_remaining'], 1372)
        self.assertEqual(report['quota']['theoretical_daily_max'], 3900)
        self.assertTrue(report['quota']['headers_complete'])
        for secret in ('private-login', 'private-key', 'private-target', 'secret.invalid', 'private-header'):
            self.assertNotIn(secret, json.dumps(report))

    def test_non_success_response_has_no_body_read_no_retry_and_safe_error(self):
        for status, code in ((401, 'access_denied'), (403, 'access_denied'), (429, 'rate_limited'),
                             (302, 'redirect_rejected'), (500, 'http_error')):
            with self.subTest(status=status):
                report, session, response = self.probe(status=status, headers={**HEADERS, 'Retry-After': '42'})
                self.assertFalse(report['probe']['ok'])
                self.assertEqual(report['probe']['error'], 'upgates_capacity_' + code)
                self.assertEqual(report['probe']['http_status'], status)
                self.assertEqual(report['quota']['retry_after_seconds'], 42 if status == 429 else None)
                response.iter_content.assert_not_called()
                session.get.assert_called_once()
                response.close.assert_called_once()

    def test_local_budget_rejection_records_no_remote_call(self):
        from inventory_hub.services.upgates_budget import _deferred
        client = SimpleNamespace(base_url='https://budget-test.invalid/api/v2',
                                 session=Mock(auth=('synthetic', 'synthetic')))
        client.session.get.return_value = _deferred(30)
        with patch.object(service.UpgatesClient, 'from_shop', return_value=client):
            report = service.probe('biketrek')
        self.assertEqual(report['probe']['requests_attempted'], 0)
        self.assertEqual(report['probe']['error'], 'upgates_capacity_rate_limited')
        self.assertEqual(report['quota']['retry_after_seconds'], 30)

    def test_invalid_upstream_payloads_and_ambiguous_permissions_are_rejected(self):
        conflict = {'services': {'products': {**BODY['services']['orders']}}, 'documentation_link': ''}
        duplicate_service = {'services': {'products': BODY['services']['products'],
            'Products': BODY['services']['products']}, 'documentation_link': ''}
        for raw in (b'{"services":{},"services":{},"documentation_link":""}', b'not-json', b'{"services":NaN}',
                    json.dumps({'services': [], 'documentation_link': ''}).encode(),
                    json.dumps(conflict).encode(), json.dumps(duplicate_service).encode(),
                    b'x' * (service.MAX_BYTES + 1)):
            with self.subTest(prefix=raw[:30]):
                report, session, response = self.probe(raw=raw)
                self.assertEqual(report['probe']['error'], 'upgates_capacity_invalid_response')
                self.assertEqual(set(report['probe']['permissions'].values()), {'unknown'})
                session.get.assert_called_once()
                response.close.assert_called_once()

    def test_unknown_privilege_is_unknown_without_exposing_raw_text(self):
        body = {'services': {'products': {**BODY['services']['products'], 'privilege': 'private-vocabulary'}},
                'documentation_link': ''}
        report, _, _ = self.probe(body=body)
        self.assertTrue(report['probe']['ok'])
        self.assertEqual(report['probe']['permissions']['products'], 'unknown')
        self.assertNotIn('private-vocabulary', json.dumps(report))

    def test_quota_missing_or_malformed_is_unknown_not_zero_or_fabricated(self):
        quota = service._quota({'X-Rate-Limit-Hour': '100', 'X-Rate-Limit-Hour-Remaining': '101',
            'X-Rate-Limit-Day': '∞', 'X-Rate-Limit-Day-Remaining': '-1', 'X-Rate-Limit-Total-Remaining': 'secret'})
        self.assertEqual(quota['hour_limit'], 100)
        self.assertIsNone(quota['hour_remaining'])
        self.assertIsNone(quota['total_remaining'])
        self.assertIsNone(quota['theoretical_daily_max'])
        self.assertFalse(quota['headers_complete'])
        for value in ('', '1.5', '1e4', '１０', '1000000001', '123456789012', ' 10 ', True, None):
            self.assertIsNone(service._numeric_header(value), repr(value))

    def test_connection_failure_does_not_retry_or_return_exception_credentials(self):
        report, session, _ = self.probe(failure=requests.Timeout('https://private-login:private-key@target'))
        session.get.assert_called_once()
        self.assertEqual(report['probe']['error'], 'upgates_capacity_source_unavailable')
        self.assertIsNone(report['probe']['http_status'])
        self.assertNotIn('private-key', json.dumps(report))

    def test_no_request_if_connection_missing_or_target_insecure(self):
        with patch.object(service.UpgatesClient, 'from_shop', side_effect=service.UpgatesError('private-key')):
            report = service.probe('biketrek')
        self.assertEqual(report['probe']['requests_attempted'], 0)
        self.assertEqual(report['probe']['error'], 'upgates_capacity_connection_missing')
        self.assertIsNone(report['probe']['latency_ms'])
        for target in ('http://insecure.invalid/api/v2', 'https://x:y@invalid/api/v2', 'https://invalid/api/v2?key=secret'):
            report, session, _ = self.probe(base=target)
            self.assertEqual(report['probe']['requests_attempted'], 0)
            self.assertEqual(report['probe']['error'], 'upgates_capacity_target_invalid')
            session.get.assert_not_called()


class CapacityRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = FastAPI()
        self.app.include_router(shops.router)

        async def session():
            yield object()

        self.app.dependency_overrides[get_session] = session
        self.token_patch = patch.object(settings, 'AI_CONTENT_ACCESS_TOKEN', SecretStr(TOKEN))
        self.token_patch.start()
        self.addCleanup(self.token_patch.stop)

    async def request(self, path='/shops/biketrek/upgates/capacity', authorized=True):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='https://hub.test') as client:
            return await client.get(path, headers={'Authorization': 'Bearer ' + TOKEN} if authorized else {})

    async def test_route_requires_operator_before_probe_and_rejects_unsafe_shop(self):
        with patch.object(service, 'diagnose', AsyncMock()) as diagnose:
            self.assertEqual((await self.request(authorized=False)).status_code, 401)
            self.assertEqual((await self.request('/shops/INVALID/upgates/capacity')).status_code, 422)
            diagnose.assert_not_awaited()

    async def test_report_is_not_cached_and_upstream_failure_remains_visible_report(self):
        report = {'shop_code': 'biketrek', 'probe': {'ok': False, 'error': 'upgates_capacity_rate_limited'},
                  'quota': {'total_remaining': 0}, 'projection': {'listed_skus': 3}}
        with patch.object(service, 'diagnose', AsyncMock(return_value=report)) as diagnose:
            response = await self.request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), report)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        diagnose.assert_awaited_once()
        self.assertEqual(diagnose.await_args.kwargs, {'include_product_sample': False})

    async def test_catalog_sample_is_explicitly_opt_in_on_protected_route(self):
        with patch.object(service, 'diagnose', AsyncMock(return_value={'catalog_probe': {'requested': True}})) as diagnose:
            response = await self.request('/shops/biketrek/upgates/capacity?include_product_sample=true')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(diagnose.await_args.kwargs, {'include_product_sample': True})

    async def test_local_errors_are_allowlisted_without_exception_detail(self):
        for error, status, code in ((service.CapacityError('upgates_capacity_shop_not_found', 404), 404,
                'upgates_capacity_shop_not_found'), (RuntimeError('private-db-url-and-key'), 503,
                'upgates_capacity_local_unavailable')):
            with patch.object(service, 'diagnose', AsyncMock(side_effect=error)):
                response = await self.request()
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.json()['detail']['code'], code)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertNotIn('private-db', response.text)


class CapacityCatalogSampleTests(unittest.IsolatedAsyncioTestCase):
    def targets(self):
        # Four maximum-length UTF-8 codes of each kind still fit into two GETs.
        targets = []
        for variant in (False, True):
            for index in range(4):
                code = '\U0001f6b2' * 98 + ('V' if variant else 'P') + str(index)
                targets.append({'code': code, 'parent_code': 'PARENT' if variant else code,
                                'variant_code': code if variant else None,
                                'product_id': 90 if variant else 100 + index,
                                'variant_id': 200 + index if variant else None})
        return targets

    async def test_success_reports_counts_without_returning_raw_product_data(self):
        targets = self.targets()
        raw = {'private-code': {'ean': 'private-ean', 'purchase_price': 'private-price',
                               'url': 'https://private-catalog.invalid'}}
        with patch.object(service.stock_sync.source, 'read_many', AsyncMock(return_value=raw)) as read:
            report = await service._sample_probe('biketrek', targets, {'ok': True, 'target_fingerprint': 'a' * 64})
        read.assert_awaited_once_with('biketrek', targets, 'a' * 64)
        self.assertEqual(report, {'requested': True, 'ok': True, 'selected_products': 4, 'selected_variants': 4,
            'planned_requests': 2, 'requests_attempted': 2, 'error': None})
        for private in ('private-code', 'private-ean', 'private-price', 'private-catalog', targets[0]['code']):
            self.assertNotIn(private, json.dumps(report, ensure_ascii=False))

    async def test_failure_has_unknown_actual_call_count_and_never_retries(self):
        failures = ((service.stock_sync.source.SourceError('stock_publication_rate_limited', 429),
                     'stock_publication_rate_limited'),
                    (RuntimeError('private-login:private-key@private-target'), 'upgates_capacity_sample_unavailable'))
        for failure, expected in failures:
            with self.subTest(error=expected), patch.object(service.stock_sync.source, 'read_many',
                    AsyncMock(side_effect=failure)) as read:
                report = await service._sample_probe('biketrek', self.targets(), {'ok': True, 'target_fingerprint': 'a' * 64})
                read.assert_awaited_once()
            self.assertFalse(report['ok'])
            self.assertEqual(report['planned_requests'], 2)
            self.assertIsNone(report['requests_attempted'], 'Failure may follow an unknown number of sent groups')
            self.assertEqual(report['error'], expected)
            self.assertNotIn('private-', json.dumps(report))

    async def test_failed_status_or_no_exact_targets_sends_no_catalog_request(self):
        for targets, status, expected in ((self.targets(), {'ok': False}, 'upgates_capacity_sample_status_required'),
                ([], {'ok': True}, 'upgates_capacity_sample_empty')):
            with self.subTest(error=expected), patch.object(service.stock_sync.source, 'read_many', AsyncMock()) as read:
                report = await service._sample_probe('biketrek', targets, status)
                read.assert_not_awaited()
            self.assertFalse(report['ok'])
            self.assertEqual(report['requests_attempted'], 0)
            self.assertEqual(report['error'], expected)
