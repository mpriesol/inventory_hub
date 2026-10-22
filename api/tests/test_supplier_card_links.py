"""Supplier identity evidence, exact writes and authenticated recovery contracts."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from inventory_hub.database import get_session
from inventory_hub.db_models import IdentifierType
from inventory_hub.db_models_ext import ProductSupplySource
from inventory_hub.routers import supplier_card_links as routes
from inventory_hub.services import supplier_card_links as service
from inventory_hub.services.product_identity import IdentityIndex
from inventory_hub.settings import settings
from inventory_hub.supplier_card_link_types import SupplierCardLinkApplyRequest, SupplierCardLinkPreviewRequest


EAN = '8585047234448'
OTHER = '5901234123457'


def source():
    return SimpleNamespace(id=11, supplier_id=5, supplier_sku='103404-281-103', ean=EAN,
        supplier_group_code='103404', name='NORTHKIT blue M', last_seen_at=service.now(),
        is_active=True, is_discontinued=False, attributes={'catalog': {'supplier': 'northfinder',
            'code': '103404-281-103', 'shop_code': 'NF-103404-281-103', 'name': 'NORTHKIT blue M',
            'group_code': '103404', 'prices': {'currency': 'EUR'}, 'eans': [EAN],
            'variant_attributes': [{'name': 'Farba', 'value': 'blue'}, {'name': 'Veľkosť', 'value': 'M'}]}})


class SupplierCardIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.facts = {'product_id': 1, 'sku': 'EXISTING-M', 'name': 'Retained name', 'is_active': True,
                      'eans': [], 'attributes': [], 'manual_eans': None}
        self.supplier = SimpleNamespace(id=5, code='northfinder', name='Northfinder', is_active=True)
        self.source = source()

    async def inspect(self, *, identifiers=(), aliases=(), links=(), other_sources=(), local_brand=None):
        db = AsyncMock()
        db.scalar.side_effect = [self.source, local_brand]
        db.get.return_value = self.supplier
        db.scalars.side_effect = [list(aliases), list(links), list(other_sources)]
        products = [SimpleNamespace(id=1, sku=self.facts['sku'], is_active=True),
                    SimpleNamespace(id=2, sku='OTHER-CARD', is_active=True)]
        own_eans = [SimpleNamespace(product_id=1, identifier_type=IdentifierType.ean,
            supplier_id=None, value=ean) for ean in self.facts['eans']]
        index = IdentityIndex(0, products, identifiers=[*own_eans, *identifiers, *aliases])
        with patch.object(service, '_product', AsyncMock(return_value=self.facts)), \
             patch.object(service, '_ean_adoption', AsyncMock(return_value={
                 'available': True, 'selected': False, 'eans': [EAN], 'reason': None, '_existing': []})), \
             patch.object(service, 'load_identity_index', AsyncMock(return_value=index)):
            return await service.inspect_link(db, 1, 11)

    async def test_explicit_different_sku_with_unknown_local_ean_axes_is_reviewable(self):
        result = await self.inspect()
        self.assertEqual(result['link_status'], 'ready')
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['planned_changes'], ['supplier_alias', 'supplier_source'])
        self.assertIn('variant_axes_missing', result['warnings'])
        self.assertIn('barcode_missing', result['warnings'])
        self.assertEqual(result['product']['sku'], 'EXISTING-M')
        self.assertEqual(result['source']['supplier_sku'], '103404-281-103')

    async def test_verified_ean_conflict_and_other_barcode_owner_remain_blocking(self):
        self.facts['eans'] = [OTHER]
        self.assertIn('identifier_conflict', (await self.inspect())['errors'])
        self.facts['eans'] = []
        owner = SimpleNamespace(product_id=2, identifier_type=IdentifierType.ean, supplier_id=None, value=EAN)
        self.assertIn('assigned_product_changed', (await self.inspect(identifiers=[owner]))['errors'])

    async def test_present_contradictory_axes_block_but_localized_equivalent_is_valid(self):
        self.facts['attributes'] = [{'name': 'Farba', 'value': 'Modrá'}, {'name': 'Size', 'value': 'M'}]
        self.assertEqual((await self.inspect())['errors'], [])
        self.facts['attributes'][1]['value'] = 'L'
        self.assertIn('variant_axes_conflict', (await self.inspect())['errors'])

    async def test_equivalent_gtins_accept_source_record_catalog_and_manual_evidence(self):
        self.source.ean = '727670929720'
        self.source.attributes['catalog']['eans'] = ['0727670929720']
        self.facts['eans'] = self.facts['manual_eans'] = ['727670929720']
        result = await self.inspect()
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['source']['eans'], ['0727670929720', '727670929720'])

    async def test_brand_conflict_blocks_and_registered_aliases_agree(self):
        from inventory_hub.services import sku_policy
        self.source.attributes['catalog']['brand'] = 'BBB Cycling'
        with patch.object(sku_policy, 'registered_brand_id', AsyncMock(side_effect=[1, 2])):
            self.assertIn('sku_brand_conflict', (await self.inspect(local_brand='Topeak'))['errors'])
        with patch.object(sku_policy, 'registered_brand_id', AsyncMock(side_effect=[7, 7])):
            result = await self.inspect(local_brand='BBB')
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['identity']['brands'], {'product': 'BBB', 'source': 'BBB Cycling'})
        unknown = await self.inspect()
        self.assertEqual(unknown['errors'], [], 'Missing brand alone is not contradictory evidence')

    async def test_alias_and_source_owners_cannot_be_overridden(self):
        alias = SimpleNamespace(product_id=2, identifier_type=IdentifierType.supplier_sku,
                                supplier_id=5, value='103404-281-103')
        for changes in ({'aliases': [alias]}, {'links': [SimpleNamespace(product_id=2)]}):
            with self.subTest(changes=changes):
                self.assertIn('supplier_source_multiple_products', (await self.inspect(**changes))['errors'])
        self.assertIn('supplier_link_already_configured', (await self.inspect(other_sources=[12]))['errors'])

    async def test_exact_existing_disabled_link_preserves_policy_and_alias_replay(self):
        before = await self.inspect()
        alias = SimpleNamespace(product_id=1, identifier_type=IdentifierType.supplier_sku,
                                supplier_id=5, value='103404-281-103')
        link = SimpleNamespace(product_id=1, **service._policy(None))
        repeated = await self.inspect(aliases=[alias], links=[link])
        self.assertEqual(repeated['preview_hash'], before['preview_hash'])
        self.assertEqual(repeated['link_status'], 'already_linked')
        link.orderable = False
        disabled = await self.inspect(aliases=[alias], links=[link])
        self.assertIn('supplier_link_disabled', disabled['warnings'])
        self.assertFalse(disabled['identity']['source_policy']['orderable'])
        self.assertNotEqual(disabled['preview_hash'], before['preview_hash'])

    async def test_draft_identity_hash_changes_with_ean_axes_and_owners_not_price(self):
        before = await self.inspect()
        self.source.attributes['catalog']['prices']['purchase_net'] = '9.99'
        self.assertEqual((await self.inspect())['preview_hash'], before['preview_hash'])
        self.source.attributes['catalog']['variant_attributes'][1]['value'] = 'L'
        self.assertNotEqual((await self.inspect())['preview_hash'], before['preview_hash'])

    async def test_cached_sku_proposal_never_owns_a_supplier_link_or_invalidates_review(self):
        before = await self.inspect()
        self.source.attributes['catalog']['shop_code'] = 'UNRELATED-CACHED-PROPOSAL'
        after = await self.inspect()
        self.assertEqual(after['errors'], [])
        self.assertEqual(after['preview_hash'], before['preview_hash'])

    def test_source_record_and_catalog_identity_must_agree(self):
        for mutation, expected in ((lambda row: setattr(row, 'ean', OTHER), 'supplier_card_links_source_ean_conflict'),
            (lambda row: row.attributes['catalog'].update(code='different'), 'supplier_card_links_source_identity_changed'),
            (lambda row: row.attributes['catalog'].update(group_code='different'), 'supplier_card_links_source_identity_changed')):
            row = source(); mutation(row)
            with self.subTest(expected=expected), self.assertRaises(service.LinkError) as error:
                service._source(row, self.supplier)
            self.assertEqual(error.exception.code, expected)

    async def test_apply_helper_adds_only_exact_relations_without_committing(self):
        inspected = await self.inspect()
        db = AsyncMock(); db.add = MagicMock()
        with patch.object(service, 'inspect_link', AsyncMock(return_value=inspected)), \
             patch.object(service.ProductIdentifierService, 'add_identifier', AsyncMock()) as add_alias:
            result = await service.apply_link(db, 1, 11, expected_hash=inspected['preview_hash'])
        self.assertEqual(result['changes'], ['supplier_alias', 'supplier_source'])
        self.assertEqual(add_alias.await_args.args[:3], (1, '103404-281-103', IdentifierType.supplier_sku))
        self.assertEqual(add_alias.await_args.kwargs['supplier_id'], 5)
        self.assertEqual(len(db.add.call_args_list), 1)
        added = db.add.call_args.args[0]
        self.assertIsInstance(added, ProductSupplySource)
        self.assertEqual((added.product_id, added.supplier_product_id), (1, 11))
        db.commit.assert_not_awaited()

    async def test_ean_adoption_only_accepts_verified_source_values_and_empty_card(self):
        db = AsyncMock(); db.scalars.return_value = []; db.scalar.return_value = None
        result = await service._ean_adoption(db, self.facts, {'eans': [EAN, '1234', '0000000000000']}, selected=True)
        self.assertEqual(result['eans'], [EAN])
        self.assertTrue(result['available'])
        self.assertTrue(result['selected'])
        db.scalars.return_value = ['1234']
        self.assertEqual((await service._ean_adoption(db, self.facts, {'eans': [EAN]}))['reason'], 'ean_present')
        db.scalars.return_value = []; self.facts['manual_eans'] = [OTHER]
        self.assertEqual((await service._ean_adoption(db, self.facts, {'eans': [EAN]}))['reason'], 'ean_present')
        self.facts['manual_eans'] = []
        self.assertTrue((await service._ean_adoption(db, self.facts, {'eans': [EAN]}))['available'])
        db.scalar.return_value = 2
        self.assertEqual((await service._ean_adoption(db, self.facts, {'eans': [EAN]}))['reason'], 'ean_conflict')

    async def test_invalid_source_digits_cannot_be_copied(self):
        db = AsyncMock(); db.scalars.return_value = []
        result = await service._ean_adoption(db, self.facts, {'eans': ['1234', '8585047234449']}, selected=True)
        self.assertFalse(result['available'])
        self.assertEqual(result['reason'], 'source_ean_missing')
        self.assertEqual(result['eans'], [])
        db.scalar.assert_not_awaited()


class SupplierCardRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        inspection = {'preview_hash': 'b'*64, 'link_status': 'ready', 'product': {}, 'source': {},
                      'warnings': [], 'errors': [], 'planned_changes': ['supplier_alias', 'supplier_source']}
        document = {'inspection': inspection}
        self.row = SimpleNamespace(id=str(uuid4()), document=document, preview_hash=service.digest(document),
            status='prepared', product_id=1, supplier_product_id=11, result=None,
            expires_at=service.now()+timedelta(minutes=20), completed_at=None)
        self.payload = SupplierCardLinkApplyRequest(preview_hash=self.row.preview_hash, confirmed=True)
        self.db = AsyncMock(); self.db.scalar.return_value = self.row

    async def test_known_identity_failure_commits_expiry_before_marker(self):
        with patch.object(service, 'apply_link', AsyncMock(side_effect=service.LinkError('changed'))):
            with self.assertRaises(service.LinkError) as error:
                await service.apply(self.db, self.row.id, self.payload)
        self.assertTrue(error.exception.known_unapplied)
        self.assertLessEqual(self.row.expires_at, service.now())
        self.db.commit.assert_awaited_once()
        self.db.execute.assert_awaited_once()  # Shared fence precedes row lock/checks.

    async def test_commit_failure_never_claims_known_unapplied(self):
        self.db.commit.side_effect = RuntimeError('synthetic uncertain commit')
        with patch.object(service, 'apply_link', AsyncMock(side_effect=service.LinkError('changed'))):
            with self.assertRaises(RuntimeError):
                await service.apply(self.db, self.row.id, self.payload)

    async def test_completed_replay_returns_saved_result_without_relation_writes(self):
        self.row.status, self.row.result = 'completed', {'status': 'linked', 'product_id': 1, 'supplier_product_id': 11}
        self.row.expires_at = service.now()-timedelta(days=1)
        with patch.object(service, 'apply_link', AsyncMock()) as apply_link:
            result = await service.apply(self.db, self.row.id, self.payload)
        self.assertEqual(result['result'], self.row.result)
        apply_link.assert_not_awaited()

    async def test_old_preview_request_replays_but_cannot_change_its_ean_choice(self):
        original = {'request_id': self.row.id, 'product_id': 1, 'supplier_product_id': 11}
        self.row.request_hash = service.digest(original)
        self.db.get.return_value = self.row
        with patch.object(service, 'inspect_link', AsyncMock()) as inspect_link:
            result = await service.preview(self.db, SupplierCardLinkPreviewRequest(**original))
            self.assertEqual(result['id'], self.row.id)
            with self.assertRaises(service.LinkError) as changed:
                await service.preview(self.db, SupplierCardLinkPreviewRequest(**original, copy_supplier_eans=True))
        self.assertEqual(changed.exception.code, 'supplier_card_links_request_changed')
        inspect_link.assert_not_awaited()


class SupplierCardRoutesTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI(); app.include_router(routes.router)
        self.db = SimpleNamespace(); self.opened = []
        async def session():
            self.opened.append(True)
            yield self.db
        app.dependency_overrides[get_session] = session
        self.client = self.enterContext(TestClient(app))
        token = 'synthetic-supplier-link-token-'+'x'*32
        self.enterContext(patch.object(settings, 'AI_CONTENT_ACCESS_TOKEN', SecretStr(token)))
        self.headers = {'Authorization': 'Bearer '+token}

    def test_unauthorized_options_cannot_read_products(self):
        with patch.object(service, 'options', AsyncMock()) as operation:
            response = self.client.get('/supplier-card-links/options?product_id=1')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(self.opened, [])
        operation.assert_not_awaited()

    def test_strict_input_rejects_creation_price_stock_and_nonliteral_confirmation(self):
        valid = {'request_id': str(uuid4()), 'product_id': 1, 'supplier_product_id': 11}
        for extra in ({'price': 1}, {'create_missing': True}, {'stock': 1}, {'product_id': True}, {'product_id': '1'}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                SupplierCardLinkPreviewRequest(**{**valid, **extra})
        for value in (1, 'true', False, None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                SupplierCardLinkApplyRequest(preview_hash='a'*64, confirmed=value)
        for value in (1, 'true', None):
            with self.subTest(copy=value), self.assertRaises(ValidationError):
                SupplierCardLinkPreviewRequest(**valid, copy_supplier_eans=value)
        self.assertTrue(SupplierCardLinkPreviewRequest(**valid, copy_supplier_eans=True).copy_supplier_eans)
        response = self.client.post('/supplier-card-links/preview', json={**valid, 'secret': 'DO-NOT-ECHO'}, headers=self.headers)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertNotIn('DO-NOT-ECHO', response.text)

    def test_domain_fence_marker_is_forwarded_without_exception_text(self):
        error = service.LinkError('supplier_card_links_identity_changed'); error.known_unapplied = True
        with patch.object(service, 'apply', AsyncMock(side_effect=error)):
            response = self.client.post('/supplier-card-links/'+str(uuid4())+'/apply',
                json={'preview_hash': 'a'*64, 'confirmed': True}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail'], {'code': error.code, 'known_unapplied': True})


if __name__ == '__main__':
    unittest.main()
