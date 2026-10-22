"""Display identities use current scoped proof, never the cached SKU proposal."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from catalog_fixtures import product
from inventory_hub.catalog_types import CatalogDisplayProduct, CatalogParameter
from inventory_hub.db_models import IdentifierType, Product, ProductIdentifier
from inventory_hub.services import catalog, catalog_hub_identity as service
from inventory_hub.services.product_identity import IdentityIndex


class CatalogHubIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_projection_ignores_legacy_proposal_and_preserves_source(self):
        products = [CatalogDisplayProduct.model_validate(product(id).model_dump()) for id in (1, 2)]
        sources = {p.id: SimpleNamespace(supplier_id=4, supplier_sku=p.code, ean=None, brand='TEST') for p in products}
        hub = Product(id=42, sku='OLD-00042', name='Current card', brand='TEST', is_active=True)
        prefix_guess = Product(id=43, sku=products[0].shop_code, name='Unrelated card', brand='TEST', is_active=True)
        alias = ProductIdentifier(product_id=42, identifier_type=IdentifierType.supplier_sku, supplier_id=4, value=products[0].code)
        index = IdentityIndex(0, products=[hub, prefix_guess], identifiers=[alias])
        db = SimpleNamespace(execute=AsyncMock(side_effect=[SimpleNamespace(all=lambda: []),
            SimpleNamespace(all=lambda: [])]))
        card = {'product_id': 42, 'sku': hub.sku, 'brand': hub.brand, 'attributes': [], 'manual_eans': None}
        with patch.object(service, 'load_identity_index', AsyncMock(return_value=index)) as load, \
                patch.object(service, '_load_cards', AsyncMock(return_value={42: card})) as cards:
            await service.attach(db, products, sources)
        self.assertEqual(load.await_count, 1)
        self.assertEqual(cards.await_count, 1)
        self.assertEqual(len(load.call_args.args[2]), 2)
        self.assertTrue(all(not identity.code for identity in load.call_args.args[2]))
        self.assertEqual((products[0].hub_identity_status, products[0].hub_sku), ('assigned', 'OLD-00042'))
        self.assertEqual(products[1].hub_identity_status, 'unassigned')
        self.assertEqual(products[0].shop_code, 'PL-A-001')
        snapshot = product().model_dump(mode='json')
        row = SimpleNamespace(id=1, attributes={'catalog': snapshot})
        self.assertEqual(catalog.public_product(row).model_dump(mode='json'), snapshot)
        self.assertNotIn('hub_sku', catalog.public_product(row).model_dump())

    def test_known_variant_brand_and_manual_barcode_contradictions_are_explicit(self):
        item = CatalogDisplayProduct.model_validate(product().model_dump())
        item.brand = 'TOPEAK'; item.eans = ['0036000291452']
        item.variant_attributes = [CatalogParameter(name='Farba', value='červená')]
        source = SimpleNamespace(supplier_sku=item.code, ean='036000291452', brand='Topeak')
        card = {'brand': 'Topeak Alias', 'attributes': [{'name': 'color', 'value': 'red'}],
                'manual_eans': ['036000291452']}
        brands = {'topeak': 1, 'topeak alias': 1}
        self.assertEqual(service._contradictions(item, source, card, brands), [])
        card['brand'] = 'Different brand'
        card['attributes'][0]['value'] = 'blue'
        card['manual_eans'] = ['5901234123457']
        self.assertCountEqual(service._contradictions(item, source, card, brands),
            ['sku_brand_conflict', 'variant_axes_conflict', 'manual_barcode_conflict'])

    def test_ambiguous_axes_and_conflicting_feed_identifiers_are_not_hidden(self):
        item = CatalogDisplayProduct.model_validate(product().model_dump())
        item.eans = ['0036000291452']
        item.variant_attributes = [CatalogParameter(name='Size', value='M'), CatalogParameter(name='Veľkosť', value='L')]
        source = SimpleNamespace(supplier_sku='different', ean='5901234123457', brand='TEST')
        card = {'brand': 'TEST', 'attributes': [], 'manual_eans': None}
        self.assertCountEqual(service._contradictions(item, source, card, {}),
            ['supplier_source_identity_changed', 'supplier_source_ean_conflict', 'variant_axes_ambiguous'])
