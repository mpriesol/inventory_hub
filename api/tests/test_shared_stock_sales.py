"""Sale quantities remain distinct from physical pieces and canonical GTINs."""
from copy import deepcopy
from contextlib import ExitStack
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from pydantic import ValidationError
from pydantic import SecretStr
from fastapi import FastAPI
from fastapi.testclient import TestClient
from inventory_hub.database import get_session
from inventory_hub.db_models import IdentifierType
from inventory_hub.services import sale_units, order_stock_ledger as ledger, order_stock_source as source
from inventory_hub.services.order_sku_aliases import AliasIndex
from inventory_hub.services.product_identity import IdentityIndex, RemoteIdentity
from inventory_hub.shared_stock_sale_types import SharedStockSalePreviewRequest
from inventory_hub.routers import shared_stock_sales as routes, upgates_sync
from inventory_hub.services import shared_stock_sales as sales, product_publication
from inventory_hub.settings import settings


PIECE_EAN, PACK_EAN = '5901234123457', '4006381333931'


def mapping(**changes):
    return SimpleNamespace(**{'id': 11, 'shop_id': 1, 'product_id': 1, 'external_code': 'PACK-8', 'is_variant': True,
        'external_product_id': '900', 'external_variant_id': '901', 'native_id': '901', 'parent_code': 'PATCHES',
        'is_listed': True, 'shared_stock': True, 'sale_to_stock_factor': Decimal(8), 'sale_barcodes': [PACK_EAN], **changes})


def request(**changes):
    return {'request_id': str(uuid4()), 'shop_code': 'biketrek', 'product_id': 1,
        'sales': [{'code': 'PACK-8', 'parent_code': 'PATCHES', 'is_variant': True,
                   'native_product_id': 900, 'native_variant_id': 901, 'sale_to_stock_factor': 8}],
        'identity_evidence': 'One loose patch; explicit sale quantity from the reviewed supplier record.',
        'same_physical_item_confirmed': True, 'base_piece_unit_confirmed': True, **changes}


def line(**changes):
    return {'line_key': str(uuid4()), 'code': 'PACK-8', 'quantity': '1', 'unit': 'ks',
            'classification': 'mapped', 'product_id': 1, 'sku': 'LOOSE-PATCH',
            'sale_key': '11', 'sale_mapping_id': 11, 'sale_to_stock_factor': '8', **changes}


class SaleUnitTests(unittest.TestCase):
    def test_sale_factors_cannot_be_inferred_rounded_or_boolean(self):
        for raw in (None, False, True, 0, -1, '1.5', 'NaN', 'Infinity', '1000001'):
            with self.subTest(value=raw), self.assertRaises(sale_units.SaleUnitError):
                sale_units.factor(raw)
        self.assertEqual([sale_units.factor(x) for x in (1, 8, 100)],
                         [Decimal(1), Decimal(8), Decimal(100)])

    def test_one_sold_pack_requires_eight_physical_pieces(self):
        lines, excluded, errors = ledger._source_lines({'lines': [line()]})
        self.assertEqual((excluded, errors), ([], []))
        self.assertEqual((lines[0]['sale_quantity'], lines[0]['quantity']), (Decimal(1), Decimal(8)))
        self.assertEqual(lines[0]['sale_to_stock_factor'], '8')

    def test_shared_single_piece_retains_its_own_leaf_key(self):
        value = mapping()
        value.sale_to_stock_factor = Decimal(1)
        self.assertEqual(sale_units.mapping_fields(value),
                         {'sale_key': '11', 'sale_mapping_id': 11, 'sale_to_stock_factor': '1'})
        value.shared_stock = False
        self.assertEqual(sale_units.mapping_fields(value), {})

    def test_partial_proof_fractional_sales_and_physical_overflow_are_blocked(self):
        invalid = [line(sale_key='12'), line(sale_mapping_id=True), line(quantity='0.5'),
                   line(quantity='1000', sale_to_stock_factor='1000000')]
        missing = line(); missing.pop('sale_mapping_id'); invalid.append(missing)
        for value in invalid:
            with self.subTest(line=value):
                lines, _, errors = ledger._source_lines({'lines': [value]})
                self.assertEqual(lines, [])
                self.assertTrue(errors)

    def test_legacy_snapshot_shape_and_piece_quantity_are_unchanged(self):
        value = {k: v for k, v in line(quantity='3').items() if k not in sale_units.FIELDS}
        lines, _, errors = ledger._source_lines({'lines': [value]})
        self.assertEqual(errors, [])
        self.assertEqual(lines[0], {'line_key': value['line_key'], 'product_id': 1,
            'sku': 'LOOSE-PATCH', 'code': 'PACK-8', 'quantity': Decimal(3)})

    def test_explicit_group_request_does_not_accept_quantities_or_ambiguous_targets(self):
        SharedStockSalePreviewRequest(**request())
        for changes in ({'base_piece_unit_confirmed': 1}, {'same_physical_item_confirmed': 'true'},
                        {'quantity': 200}, {'identity_evidence': 'unknown'}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                SharedStockSalePreviewRequest(**request(**changes))
        for raw in (0, True, '8', 1.5, 1000001):
            payload = request(); payload['sales'][0]['sale_to_stock_factor'] = raw
            with self.subTest(factor=raw), self.assertRaises(ValidationError):
                SharedStockSalePreviewRequest(**payload)
        payload = request(); payload['sales'].append(deepcopy(payload['sales'][0]))
        with self.assertRaises(ValidationError):
            SharedStockSalePreviewRequest(**payload)
        payload = request()
        payload['sales'][0]['replace_deleted_mapping'] = {'mapping_id': True, 'native_product_id': 97148}
        with self.assertRaises(ValidationError):
            SharedStockSalePreviewRequest(**payload)
        payload['sales'][0]['replace_deleted_mapping']['mapping_id'] = 7
        extra = {**deepcopy(payload['sales'][0]), 'code': 'SECOND', 'native_variant_id': 902}
        payload['sales'].append(extra)
        with self.assertRaises(ValidationError):
            SharedStockSalePreviewRequest(**payload)


class SaleIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.products = [SimpleNamespace(id=1, sku='LOOSE-PATCH', is_active=True, item_type='stock', stock_unit='pcs'),
                         SimpleNamespace(id=2, sku='TEST-OLD-PACK', is_active=False, item_type='stock', stock_unit='pack')]
        self.map = mapping()
        identifiers = [SimpleNamespace(product_id=1, identifier_type=IdentifierType.ean, value=PIECE_EAN),
                       SimpleNamespace(product_id=2, identifier_type=IdentifierType.ean, value=PACK_EAN)]
        self.index = IdentityIndex(1, self.products, [self.map], identifiers)

    def test_verified_sale_gtin_is_not_mistaken_for_a_base_piece_gtin(self):
        result = self.index.resolve(RemoteIdentity(1, code='PACK-8', barcodes=(PACK_EAN,)))
        self.assertEqual((result.status, result.product_id, result.mapping_id), ('mapped', 1, 11))
        wrong = self.index.resolve(RemoteIdentity(1, code='PACK-8', barcodes=(PIECE_EAN,)))
        self.assertEqual(wrong.status, 'conflict')
        # A barcode alone never creates a shared-piece relation.
        self.assertNotEqual(self.index.resolve(RemoteIdentity(1, barcodes=(PACK_EAN,))).product_id, 1)

    def test_shared_mapping_cannot_convert_a_pack_card_or_service(self):
        for field, value in (('stock_unit', 'pack'), ('item_type', 'service')):
            before = getattr(self.products[0], field)
            setattr(self.products[0], field, value)
            self.assertEqual(self.index.resolve(RemoteIdentity(1, code='PACK-8', barcodes=(PACK_EAN,))).status, 'conflict')
            setattr(self.products[0], field, before)

    def test_other_shop_requires_explicit_factor_but_keeps_existing_ordinary_mapping(self):
        index = IdentityIndex(2, self.products, [self.map], shared_product_ids={1})
        resolved = index.resolve(RemoteIdentity(2, code='LOOSE-PATCH'))
        self.assertEqual(resolved.reasons, ['shared_stock_sale_requires_group_workflow'])
        ordinary = mapping(id=12, shop_id=2, external_code='LOOSE-PATCH', shared_stock=False,
                           sale_to_stock_factor=Decimal(1))
        index.add_mapping(ordinary)
        self.assertEqual(index.resolve(RemoteIdentity(2, code='LOOSE-PATCH')).status, 'mapped')
        self.assertEqual(sale_units.mapping_fields(ordinary), {})
        local = IdentityIndex(0, self.products, shared_product_ids={1})
        self.assertEqual(local.resolve(RemoteIdentity(0, code='LOOSE-PATCH')).status, 'identified')

    async def test_saved_or_injected_factor_is_discarded_and_rederived(self):
        raw = source._line({'uuid': str(uuid4()), 'code': 'PACK-8', 'title': 'Eight patches',
                            'ean': PACK_EAN, 'quantity': '1', 'unit': 'ks', 'type': 'product'})
        raw.update(sale_key='999', sale_mapping_id=999, sale_to_stock_factor='100')
        with patch.object(source, 'load_identity_index', AsyncMock(return_value=self.index)), \
             patch.object(source.order_sku_aliases, 'load', AsyncMock(return_value=AliasIndex(self.index, 'biketrek', None, {}))):
            resolved = await source.resolve_lines(None, SimpleNamespace(id=1), {'lines': [raw]})
        self.assertEqual(resolved[0]['quantity'], '1')
        self.assertEqual((resolved[0]['sale_key'], resolved[0]['sale_mapping_id'],
                          resolved[0]['sale_to_stock_factor']), ('11', 11, '8'))

    def test_ordinary_pull_cannot_copy_pack_identifiers_to_the_piece_card(self):
        family = upgates_sync._pull_families(1, [{'product_id': 900, 'code': 'PATCHES',
            'variants': [{'variant_id': 901, 'code': 'PACK-8', 'ean': PACK_EAN}]}])[0]
        _, conflict = upgates_sync._family_resolution(family, self.index)
        self.assertIn('shared_stock_sale_requires_group_workflow', conflict['reasons'])

    def test_ordinary_merchandising_publication_cannot_choose_a_sale_sibling(self):
        row = {'shops': [{'shop_code': 'biketrek', 'mapped': True, 'mapping': None,
                          'shared_stock': True, 'sale_mappings': [{'id': 11}, {'id': 12}]}]}
        with self.assertRaisesRegex(product_publication.ERROR, 'product_publication_shared_sales'):
            product_publication.target(row, 'biketrek')


class SharedSaleRouteTests(unittest.TestCase):
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
        token = 'synthetic-shared-sale-operator-' + 'x' * 24
        patcher = patch.object(settings, 'AI_CONTENT_ACCESS_TOKEN', SecretStr(token))
        patcher.start(); self.addCleanup(patcher.stop)
        self.headers = {'Authorization': 'Bearer ' + token}
        identifier = str(uuid4())
        self.calls = [('POST', '/shared-stock-sales/preview', request(), 'preview'),
                      ('GET', '/shared-stock-sales/'+identifier, None, 'get'),
                      ('POST', '/shared-stock-sales/'+identifier+'/apply',
                       {'preview_hash': 'a'*64, 'confirmed': True}, 'apply')]

    def test_auth_precedes_database_and_remote_reads_on_every_route(self):
        with ExitStack() as stack:
            operations = [stack.enter_context(patch.object(sales, name, AsyncMock())) for *_, name in self.calls]
            for method, path, body, _ in self.calls:
                response = self.client.request(method, path, json=body)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(self.db_calls, [])
            for operation in operations:
                operation.assert_not_awaited()

    def test_authenticated_exact_requests_and_validation_do_not_echo_input(self):
        for method, path, body, name in self.calls:
            with self.subTest(path=path), patch.object(sales, name, AsyncMock(return_value={'ok': True})) as operation:
                response = self.client.request(method, path, json=body, headers=self.headers)
                self.assertEqual(response.status_code, 200, response.text)
                operation.assert_awaited_once()
                self.assertEqual(response.headers['cache-control'], 'no-store')
        response = self.client.post('/shared-stock-sales/preview', headers=self.headers,
                                    json=request(unexpected='private-test-input'))
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('private-test-input', response.text)

    def test_cookie_writes_require_same_origin_before_service_or_database(self):
        @self.app.middleware('http')
        async def principal(request, call_next):
            request.state.hub_user = {'id': 7, 'role': 'operator'}
            request.state.hub_bearer = False
            return await call_next(request)
        for method, path, body, name in self.calls:
            if method != 'POST':
                continue
            with patch.object(sales, name, AsyncMock(return_value={'ok': True})) as operation:
                before = len(self.db_calls)
                response = self.client.post(path, json=body)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(len(self.db_calls), before)
                operation.assert_not_awaited()
                response = self.client.post(path, json=body,
                    headers={'Origin': 'http://testserver', 'X-Hub-Request': '1'})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(operation.await_args.kwargs['actor'], {'user_id': 7})
