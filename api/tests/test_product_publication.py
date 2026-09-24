"""Exact-leaf payloads, price basis, remote contracts and operator route boundaries."""
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from pydantic import ValidationError
from inventory_hub.product_editor_types import EditorRowPatch, PublicationResolveRequest, PublicationSendRequest
from inventory_hub.services import product_editor as editor, product_publication_source as source
from inventory_hub.services.upgates import UpgatesParameterError, variant_attributes


def remote():
    return {'identity': {'code': 'SKU-1', 'parent_code': 'POS-xTrek', 'variant_code': 'SKU-1', 'product_id': 10, 'variant_id': 20},
        'options': {'language': 'sk', 'currency': 'EUR', 'pricelist': 'Default', 'prices_with_vat': False},
        'leaf': {'code': 'SKU-1', 'ean': '8588000000016', 'active_yn': True,
            'prices': [{'language': 'sk', 'vat': Decimal('23'), 'pricelists': [{'name': 'Default',
                'price_original': Decimal('100'), 'product_discount': Decimal('10.0'), 'price_sale': Decimal('80.00')},
                {'name': 'Wholesale', 'price_original': Decimal('70')}]}]}}


def local():
    return {'eans': ['8588000000016'], 'attributes': [{'name': 'Size', 'value': 'M'}],
        'image_url': 'https://images.example.test/leaf.jpg', 'variant': {'vat_rate': '23.00'}}


class ProductPublicationPureTests(TestCase):
    def test_variant_price_is_decimal_converted_and_targets_only_leaf_without_discounts_or_siblings(self):
        observation = source.plain(remote())
        shop = {'effective': {'name': 'New', 'sale_price_gross': '147.60', 'visible': False}}
        result = source.build_patch(local(), shop, observation, ['sale_price_gross', 'visible'])
        self.assertEqual(result['payload'], {'products': [{'code': 'POS-xTrek', 'variants': [{'code': 'SKU-1',
            'prices': [{'language': 'sk', 'pricelists': [{'name': 'Default', 'price_original': 120.0}]}], 'active_yn': False}]}]})
        self.assertEqual(result['before']['sale_price_gross'], '123.00')
        self.assertEqual(result['after']['sale_price_gross'], '147.60')
        self.assertEqual(result['after']['price_sale'], '80.00')
        self.assertEqual(result['after']['product_discount'], '10')
        readback = deepcopy(observation)
        readback['leaf']['prices'][0]['pricelists'][0]['price_original'] = '120'
        readback['leaf']['active_yn'] = False
        self.assertEqual(source.field_values(readback, ['sale_price_gross', 'visible'], observation['options']), result['after'])

    def test_variant_name_and_unknown_vat_never_rewrite_parent_or_guess_tax(self):
        observation = source.plain(remote())
        shop = {'effective': {'name': 'Leaf name', 'sale_price_gross': '123', 'visible': True}}
        with self.assertRaisesRegex(source.source.SourceError, 'variant_name_unsupported'):
            source.build_patch(local(), shop, observation, ['name'])
        observation['leaf']['prices'][0]['vat'] = None
        with self.assertRaisesRegex(source.source.SourceError, 'price_unknown'):
            source.build_patch(local(), shop, observation, ['sale_price_gross'])
        observation = source.plain(remote())
        row = local(); row['variant']['vat_rate'] = '20.00'
        with self.assertRaisesRegex(source.source.SourceError, 'vat_changed'):
            source.build_patch(row, shop, observation, ['sale_price_gross'])

    def test_targeted_read_does_not_expand_pos_parent_and_checks_exact_leaf_identity(self):
        observed = remote()
        target = observed['identity']
        leaf = {**observed['leaf'], 'product_id': 10, 'variant_id': 20, 'product_code': 'POS-xTrek', 'stock': 'PRIVATE-STOCK'}
        payload = {'current_page': 1, 'current_page_items': 1, 'number_of_items': 1, 'number_of_pages': 1, 'variants': [leaf]}
        with patch.object(source.source, '_request', return_value=payload) as call:
            result = source._read(object(), target)
            self.assertEqual(result['identity'], target)
            self.assertEqual(call.call_args.args[2], 'products/variants')
            self.assertEqual(call.call_args.kwargs['params']['variant_codes'], 'SKU-1')
        payload['variants'][0]['product_code'] = 'OTHER-PARENT'
        with patch.object(source.source, '_request', return_value=payload), self.assertRaisesRegex(source.source.SourceError, 'identity_changed'):
            source._read(object(), target)

    def test_documented_and_legacy_parameters_preserve_exact_leaf_axes(self):
        expected = [{'name': 'Farba', 'value': 'Modrá'}, {'name': 'Veľkosť', 'value': 'M'}]
        new = {'parameters_new': [
            {'descriptions': [{'language': 'en', 'name': 'Color'}, {'language': 'sk', 'name': 'Farba'}],
             'values': [{'descriptions': [{'language': 'sk', 'value': 'Modrá'}]}]},
            {'descriptions': [{'language': 'sk', 'name': 'Veľkosť'}], 'values': [{'descriptions': [{'language': 'sk', 'value': 'M'}]}]}]}
        self.assertEqual(variant_attributes(new), expected)
        self.assertEqual(variant_attributes({'parameters': [{'name': {'sk': a['name']}, 'values': [{'sk': a['value']}]} for a in expected]}), expected)
        self.assertEqual(variant_attributes({'parameters': expected}), expected)
        self.assertEqual(variant_attributes({'parameters': None}), [])
        ambiguous = {'parameters': [{'name': 'Size', 'value': 'L'}, {'name': 'Size', 'value': 'XL'}]}
        with self.assertRaises(UpgatesParameterError):
            variant_attributes(ambiguous, strict=True)
        self.assertEqual(variant_attributes(ambiguous), [])
        for malformed in ({'parameters': 'invalid'}, {'parameters': [{'name': 'Size'}]}):
            with self.assertRaises(UpgatesParameterError):
                variant_attributes(malformed, strict=True)

    def test_operator_confirmation_and_identifiers_are_exact(self):
        for value in (1, 'true', False):
            with self.assertRaises(ValidationError):
                PublicationSendRequest(confirmed=value)
            with self.assertRaises(ValidationError):
                PublicationResolveRequest(confirmed=True, original_request_settled=value, note='Confirmed settled')
        body = {'product_id': 1, 'expected_revision': 0, 'snapshot_hash': 'a' * 64}
        self.assertEqual(EditorRowPatch(**body, variant={'eans': []}).variant.eans, [])
        for code in ('1234567890123', '0000000000000', ' 12345678', 'not-ean'):
            with self.assertRaises(ValidationError):
                EditorRowPatch(**body, variant={'eans': [code]})
        with self.assertRaises(ValidationError):
            EditorRowPatch(**body, variant={'attributes': [{'name': 'Size', 'value': 'M'}, {'name': 'size', 'value': 'L'}]})


class ProductPublicationAuthTests(TestCase):
    def test_new_external_actions_and_recovery_authenticate_before_database_or_transport(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from pydantic import SecretStr
        from inventory_hub.database import get_session
        from inventory_hub.routers import product_editor as routes
        from inventory_hub.settings import settings
        from unittest.mock import AsyncMock
        from uuid import uuid4
        touched = []
        async def session():
            touched.append(True)
            yield SimpleNamespace()
        app = FastAPI(); app.include_router(routes.router)
        app.dependency_overrides[get_session] = session
        identifier = str(uuid4())
        paths = [('POST', '/product-editor/products/1/publication/preview', {'shop_code': 'biketrek', 'expected_revision': 0, 'fields': ['visible']}),
                 ('POST', '/product-editor/publications/' + identifier + '/send', {'confirmed': True}),
                 ('POST', '/product-editor/publications/' + identifier + '/resolve', {'confirmed': True, 'original_request_settled': True, 'note': 'Settled by operator'}),
                 ('GET', '/product-editor/publications/' + identifier, None),
                 ('GET', '/product-editor/products/1/publications', None),
                 ('POST', '/product-editor/products/1/refresh-attributes', None)]
        with patch.object(settings, 'AI_CONTENT_ACCESS_TOKEN', SecretStr('test-only-token-' + 'x' * 32)), TestClient(app) as client:
            for method, path, body in paths:
                response = client.request(method, path, json=body)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(touched, [])
