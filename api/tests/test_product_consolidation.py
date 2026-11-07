"""Strict whole-unit review and authenticated, non-echoing API boundary."""
from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from inventory_hub.database import get_session
from inventory_hub.product_consolidation_types import ProductConsolidationPreviewRequest
from inventory_hub.routers import product_consolidation as routes
from inventory_hub.services import product_consolidation as service, product_links
from inventory_hub.settings import settings


def body():
    return {'request_id': str(uuid4()), 'keeper_product_id': 1, 'retired_product_ids': [2],
        'mode': 'identity_only_preserve_unconfirmed_history', 'same_physical_retail_unit_confirmed': True,
        'legacy_initial_history_preserved': True,
        'identity_evidence': {'review_note': 'Verified synthetic manufacturer article: same complete retail unit.',
            'physical_unit': 'One complete synthetic set', 'source_urls': ['https://manufacturer.example/synthetic']},
        'mapping_actions': [], 'additional_sales': [{'shop_code': 'biketrek', 'code': 'SYN-SET',
            'parent_code': 'SYN-SET', 'is_variant': False, 'native_product_id': 101, 'sale_to_stock_factor': 1}]}


class ProductConsolidationInputTests(unittest.TestCase):
    def test_only_unchanged_complete_unit_factor_and_literal_confirmations_are_accepted(self):
        self.assertEqual(ProductConsolidationPreviewRequest(**body()).additional_sales[0].sale_to_stock_factor, 1)
        for factor in (0, 2, 8, True, 1.0, '1', None):
            request = body(); request['additional_sales'][0]['sale_to_stock_factor'] = factor
            with self.subTest(factor=factor), self.assertRaises(ValidationError):
                ProductConsolidationPreviewRequest(**request)
        for name in ('same_physical_retail_unit_confirmed', 'legacy_initial_history_preserved'):
            for value in (False, 1, 'true', None):
                request = body(); request[name] = value
                with self.subTest(field=name, value=value), self.assertRaises(ValidationError):
                    ProductConsolidationPreviewRequest(**request)

    def test_duplicate_native_owners_incomplete_actions_and_credential_urls_are_rejected(self):
        for replacement in ({'retired_product_ids': [1]}, {'retired_product_ids': [2, 2]},
                            {'retired_product_ids': ['2']}, {'unexpected_stock_transfer': 5}):
            with self.subTest(replacement=replacement), self.assertRaises(ValidationError):
                ProductConsolidationPreviewRequest(**{**body(), **replacement})
        request = body()
        target = request['additional_sales'][0]
        request['mapping_actions'] = [{'mapping_id': 4, 'action': 'bind', 'target': deepcopy(target)}]
        with self.assertRaises(ValidationError):
            ProductConsolidationPreviewRequest(**request)
        request = body(); request['mapping_actions'] = [{'mapping_id': 4, 'action': 'retain'}]
        with self.assertRaises(ValidationError):
            ProductConsolidationPreviewRequest(**request)
        for url in ('http://manufacturer.example/item', 'https://user:secret@manufacturer.example/item'):
            request = body(); request['identity_evidence']['source_urls'] = [url]
            with self.subTest(url=url), self.assertRaises(ValidationError):
                ProductConsolidationPreviewRequest(**request)


class ProductConsolidationRouteTests(unittest.TestCase):
    def setUp(self):
        self.db_calls = []
        async def session():
            self.db_calls.append(True)
            yield SimpleNamespace()
        self.app = FastAPI()
        self.app.include_router(routes.router)
        self.app.dependency_overrides[get_session] = session
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        token = 'synthetic-consolidation-operator-' + 'x' * 24
        configured = patch.object(settings, 'AI_CONTENT_ACCESS_TOKEN', SecretStr(token))
        configured.start(); self.addCleanup(configured.stop)
        self.headers = {'Authorization': 'Bearer ' + token}
        identifier = str(uuid4())
        self.requests = [('POST', '/product-consolidations/preview', body(), 'preview'),
            ('GET', '/product-consolidations/' + identifier, None, 'get'),
            ('POST', '/product-consolidations/' + identifier + '/apply',
             {'preview_hash': 'a' * 64, 'confirmed': True}, 'apply')]

    def test_every_route_authenticates_before_db_or_source_and_never_caches(self):
        with ExitStack() as stack:
            operations = [stack.enter_context(patch.object(service, name, AsyncMock()))
                          for *_, name in self.requests]
            for method, path, data, _ in self.requests:
                response = self.client.request(method, path, json=data)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(self.db_calls, [])
            for operation in operations:
                operation.assert_not_awaited()
        for method, path, data, name in self.requests:
            with patch.object(service, name, AsyncMock(return_value={'ok': True})) as operation:
                response = self.client.request(method, path, json=data, headers=self.headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.headers['cache-control'], 'no-store')
                operation.assert_awaited_once()

    def test_invalid_input_does_not_echo_private_fields_and_unapplied_conflict_is_explicit(self):
        invalid = {**body(), 'unexpected_secret': 'do-not-echo-this-value'}
        response = self.client.post('/product-consolidations/preview', json=invalid, headers=self.headers)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {'detail': {'code': 'consolidation_invalid_request'}})
        error = product_links.LinkError('consolidation_identity_writer_busy')
        error.known_unapplied = True
        with patch.object(service, 'apply', AsyncMock(side_effect=error)):
            method, path, data, _ = self.requests[2]
            response = self.client.request(method, path, json=data, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail'], {'code': error.code, 'known_unapplied': True})

    def test_cookie_apply_requires_shared_same_origin_protection(self):
        @self.app.middleware('http')
        async def principal(request, call_next):
            request.state.hub_user = {'id': 7, 'role': 'operator'}
            request.state.hub_bearer = False
            return await call_next(request)
        _, path, data, _ = self.requests[2]
        with patch.object(service, 'apply', AsyncMock(return_value={'ok': True})) as operation:
            rejected = self.client.post(path, json=data)
            self.assertEqual(rejected.status_code, 403)
            self.assertEqual(self.db_calls, [])
            operation.assert_not_awaited()
            accepted = self.client.post(path, json=data, headers={'Origin': 'http://testserver',
                'X-Hub-Request': '1', 'Sec-Fetch-Site': 'same-origin'})
            self.assertEqual(accepted.status_code, 200, accepted.text)
            self.assertEqual(operation.await_args.kwargs['actor'], {'user_id': 7})
