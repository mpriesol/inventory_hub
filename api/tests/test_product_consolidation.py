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
    def test_supplier_gtin_requires_a_strict_optin_without_client_exemption_fields(self):
        for value in (1, 0, 'true', 'false', None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ProductConsolidationPreviewRequest(**{**body(), 'use_keeper_supplier_gtin': value})
        self.assertFalse(ProductConsolidationPreviewRequest(**body()).use_keeper_supplier_gtin)
        for key in ('supplier_gtin_proof', 'verified_gtin_keys', 'ignore_barcode_owner', 'ignore_axes'):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                ProductConsolidationPreviewRequest(**{**body(), key: True})

    def test_unverified_claim_retirement_is_explicit_and_cannot_supply_old_identity_or_target(self):
        request = body()
        request['additional_sales'] = []
        action = {'mapping_id': 4, 'action': 'retire_unverified_code_absent', 'target': None}
        request['mapping_actions'] = [action]
        self.assertEqual(ProductConsolidationPreviewRequest(**request).mapping_actions[0].action,
                         'retire_unverified_code_absent')
        for change in ({'target': body()['additional_sales'][0]}, {'old_native_product_id': 901},
                       {'external_code': 'SYN-OLD'}, {'legacy_external_id': None}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                ProductConsolidationPreviewRequest(**{**request, 'mapping_actions': [{**action, **change}]})

    def test_unverified_plan_requires_observed_sql_nulls_and_unchanged_standalone_kind(self):
        request = body()
        request['additional_sales'] = []
        request['mapping_actions'] = [{'mapping_id': 4, 'action': 'retire_unverified_code_absent', 'target': None}]
        row = {'id': 4, 'product_id': 2, 'shop_code': 'biketrek', 'external_code': 'SYN-OLD',
               'is_variant': False, 'parent_code': None, 'external_product_id': None,
               'external_variant_id': None, 'legacy_external_id': None,
               'legacy_external_id_column_present': True, 'legacy_external_id_sql_null': True}
        plan = service._plan(request, {'mappings': [row]})
        self.assertEqual(plan['absence'], {})
        claim = plan['unverified_code_absence']['biketrek'][0]
        self.assertEqual(claim['old_native_identity_status'], 'unverified')
        self.assertIs(claim['native_id_absence_proven'], False)
        self.assertNotIn('exact_code_absence_proven', claim)
        for change in ({'is_variant': True}, {'is_variant': 0}, {'parent_code': 'PARENT'},
                       {'external_product_id': '901'}, {'external_variant_id': '902'},
                       {'legacy_external_id': ''}, {'legacy_external_id': '901'},
                       {'legacy_external_id_column_present': False}, {'legacy_external_id_sql_null': False},
                       {'external_code': None}, {'external_code': 'SYN-OLD;OTHER'}):
            with self.subTest(change=change), self.assertRaisesRegex(
                    product_links.LinkError, 'consolidation_unverified_standalone_claim_required'):
                service._plan(request, {'mappings': [{**row, **change}]})
        for key in ('legacy_external_id', 'legacy_external_id_column_present', 'legacy_external_id_sql_null'):
            incomplete = {name: value for name, value in row.items() if name != key}
            with self.subTest(missing=key), self.assertRaises(product_links.LinkError):
                service._plan(request, {'mappings': [incomplete]})
        request['additional_sales'] = [{**body()['additional_sales'][0], 'code': 'syn-old'}]
        with self.assertRaisesRegex(product_links.LinkError, 'consolidation_absent_code_selected_as_target'):
            service._plan(request, {'mappings': [row]})

    def test_keeper_preservation_requires_strict_boolean_and_has_no_client_exemption_ids(self):
        for value in (1, 0, 'true', 'false', None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ProductConsolidationPreviewRequest(**{**body(), 'preserve_keeper_invoice_receiving': value})
        self.assertFalse(ProductConsolidationPreviewRequest(**body()).preserve_keeper_invoice_receiving)
        for key in ('allowed_invoice_line_ids', 'allowed_receiving_line_ids', 'ignored_dependencies'):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                ProductConsolidationPreviewRequest(**{**body(), key: [1]})

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
