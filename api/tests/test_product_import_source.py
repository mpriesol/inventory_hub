"""Sloger preparation is ordinary source ingestion before optional AI."""
import unittest
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from catalog_fixtures import product
from test_product_import import document, row
from inventory_hub.product_import_types import DraftRevision
from inventory_hub.services import ai_content, catalog_import, product_import, product_import_source as source
from inventory_hub.services.catalog import CatalogError
from inventory_hub.services.catalog_merchandising import apply_availability
from inventory_hub.sloger_probe_types import SlogerObservation, SlogerBatchResult, SlogerLookupResult


def observation(sku='1', **changes):
    return SlogerObservation(**{'sku': sku, 'manufacturer_code': 'MODEL-1', 'eans': ['0763477003706'],
        'purchase_price': '5.1', 'retail_price': '8.5', 'currency': 'EUR', 'price_basis': 'net',
        'retail_price_basis': 'gross', 'availability': 'Skladom 8 ks', 'available': True,
        'quantity': '8', 'quantity_kind': 'exact', 'quantity_relation': 'equal',
        'source_url': 'https://www.sloger.sk/shop/brush.html', 'observed_at': ai_content.now(), **changes})


def sloger_product():
    value = product(1).model_copy(update={'supplier': 'sloger', 'code': '1', 'shop_code': 'SLG-1'})
    value.prices.purchase_net = value.prices.purchase_gross = None
    return value


def draft(rows=1):
    items = []
    for number in range(1, rows + 1):
        item = row(number, supplier_code=str(number), code=f'SLG-{number}', purchase_net=None)
        item['source'].update(supplier='sloger', code=str(number), shop_code=f'SLG-{number}')
        item['source']['prices'].update(purchase_net=None, purchase_gross=None)
        items.append(item)
    doc = document(items)
    source.initialize(doc)
    return SimpleNamespace(id='a' * 32, supplier='sloger', shop='biketrek', revision=1,
        status='source_pending', document=doc, updated_at=ai_content.now())


class ProductImportSourceTests(unittest.TestCase):
    def test_verified_eans_and_quote_augment_feed_without_repricing_dmoc(self):
        item = sloger_product()
        old = item.model_dump(mode='json')
        enriched = source.enrich_product(item, observation())
        self.assertEqual(enriched.prices.retail_gross, item.prices.retail_gross)
        self.assertEqual(enriched.prices.purchase_net, Decimal('5.1'))
        self.assertEqual(enriched.prices.purchase_gross, Decimal('6.273'))
        self.assertEqual(enriched.eans, list(dict.fromkeys([*item.eans, '0763477003706'])))
        self.assertEqual(enriched.shop_code, 'SLG-1')
        self.assertEqual(enriched.sloger_observation.source_url, observation().source_url)
        self.assertEqual(item.model_dump(mode='json'), old)

    def test_web_retail_is_only_missing_dmoc_fallback_and_currency_is_checked(self):
        item = sloger_product()
        item.prices.retail_gross = item.prices.retail_net = None
        self.assertEqual(source.enrich_product(item, observation()).prices.retail_gross, Decimal('8.5'))
        mismatched = source.enrich_product(item, observation(currency='CZK'))
        self.assertIsNone(mismatched.prices.purchase_net)
        self.assertIsNone(mismatched.prices.retail_gross)
        self.assertIn('sloger_currency_mismatch', mismatched.warnings)

    def test_unknown_or_zero_observation_clears_all_stale_positive_stock(self):
        item = sloger_product().model_copy(update={'supplier_stock': Decimal(99),
            'supplier_stock_min': Decimal(20), 'supplier_external_available': True,
            'supplier_stock_external': Decimal(100)})
        for available, quantity in ((None, None), (False, '0')):
            enriched = source.enrich_product(item, observation(available=available, quantity=quantity))
            payload = {}
            apply_availability(payload, [enriched], {'orderable': 'do 2-4 dní', 'unknown': 'overíme'})
            self.assertEqual(payload['availability'], 'overíme')
            self.assertIsNone(enriched.supplier_stock_min)
            self.assertIsNone(enriched.supplier_external_available)

    def test_strict_greater_than_zero_is_available_without_fabricated_quantity(self):
        enriched = source.enrich_product(sloger_product(), observation(quantity='0', quantity_kind='minimum', quantity_relation='greater_than'))
        payload = {}
        apply_availability(payload, [enriched], {'orderable': 'do 2-4 dní', 'unknown': 'overíme'})
        self.assertEqual(payload['availability'], 'do 2-4 dní')
        self.assertIsNone(enriched.supplier_stock)
        self.assertEqual(enriched.supplier_stock_min, Decimal(0))
        self.assertEqual(enriched.sloger_observation.quantity_relation, 'greater_than')

    def test_evidence_for_another_sku_is_rejected(self):
        with self.assertRaises(CatalogError):
            source.enrich_product(sloger_product(), observation('OTHER'))

    def test_pending_failed_and_interrupted_sources_block_the_shared_mutation_guard(self):
        item = draft()
        for state in ('queued', 'running', 'failed'):
            item.document['source_lookup']['state'] = state
            with self.assertRaises(CatalogError):
                product_import.assert_editable(item)
        item.document['source_lookup'].update(state='running', lease_expires_at=(ai_content.now() - timedelta(seconds=1)).isoformat())
        status = source.public_status(item)
        self.assertEqual(status['error'], 'import_source_interrupted')
        self.assertTrue(status['retryable'])
        self.assertNotIn('token', status)
        item.document['source_lookup']['state'] = 'completed'
        product_import.assert_editable(item)

    def test_expired_preview_downgrades_only_derived_availability(self):
        item = draft()
        target = item.document['rows'][0]
        target['source']['sloger_observation'] = observation().model_dump(mode='json')
        target['source']['supplier_available'] = True
        target['source']['supplier_observation_expires_at'] = (ai_content.now() - timedelta(seconds=1)).isoformat()
        target['values']['availability'] = 'do 2-4 dní'
        frozen = deepcopy(target)
        deadline = source.prepare_availability([target], {'unknown': 'overíme'})
        self.assertIsNone(deadline)
        self.assertEqual(target['values']['availability'], 'overíme')
        frozen['manual_fields'] = ['availability']
        source.prepare_availability([frozen], {'unknown': 'overíme'})
        self.assertEqual(frozen['values']['availability'], 'do 2-4 dní')

    def test_positive_availability_preview_has_a_deadline_checked_before_send(self):
        item = draft()
        target = item.document['rows'][0]
        target['source'].update(sloger_observation=observation().model_dump(mode='json'), supplier_available=True,
            supplier_observation_expires_at=(ai_content.now() + timedelta(hours=1)).isoformat())
        target['values']['availability'] = 'do 2-4 dní'
        self.assertIsNotNone(source.prepare_availability([target], {'unknown': 'overíme'}))
        with self.assertRaises(CatalogError) as expired:
            source.assert_availability_fresh({'supplier_availability_expires_at': (ai_content.now() - timedelta(seconds=1)).isoformat()})
        self.assertEqual(expired.exception.code, 'import_source_availability_expired')


class ProductImportSourceWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_shorter_current_ttl_caps_preview_and_publication_without_reviving_old_expiry(self):
        item = draft()
        target = item.document['rows'][0]
        observed = ai_content.now() - timedelta(hours=1)
        target['source'].update(sloger_observation=observation(observed_at=observed).model_dump(mode='json'),
            supplier_available=True, supplier_observation_expires_at=(observed + timedelta(hours=6)).isoformat())
        target['values']['availability'] = 'do 2-4 dní'
        with patch.object(source, '_freshness_seconds', AsyncMock(return_value=300)):
            self.assertEqual(await source.availability_snapshot(object(), [target], {'unknown': 'overíme'}), {})
            self.assertEqual(target['values']['availability'], 'overíme')
            with self.assertRaises(CatalogError):
                await source.assert_current_availability(object(), {
                    'supplier_availability_expires_at': target['source']['supplier_observation_expires_at'],
                    'supplier_availability_observed_at': observed.isoformat()})
        with patch.object(source, '_freshness_seconds', AsyncMock(return_value=86400)):
            with self.assertRaises(CatalogError):
                await source.assert_current_availability(object(), {
                    'supplier_availability_expires_at': (observed + timedelta(minutes=1)).isoformat(),
                    'supplier_availability_observed_at': observed.isoformat()})

    def fixture(self, item):
        @asynccontextmanager
        async def sessions():
            yield SimpleNamespace(flush=AsyncMock())
        async def record(db, observations, id):
            return {value.sku: value.observed_at + timedelta(hours=6) for value in observations}
        self.patches = [patch.object(source, 'get_session_context', sessions),
            patch.object(product_import, 'get_draft', AsyncMock(return_value=item)),
            patch.object(catalog_import, 'canonical_products', AsyncMock(side_effect=lambda db, products: products)),
            patch.object(source, 'supplier_config', return_value={'adapter_settings': {'availability': {'orderable': 'do 2-4 dní', 'unknown': 'overíme'}}}),
            patch('inventory_hub.services.supplier_availability.record_sloger_import', side_effect=record)]
        for item_patch in self.patches:
            item_patch.start()
            self.addCleanup(item_patch.stop)

    async def test_chunks_skip_successful_rows_on_explicit_retry_and_never_call_ai(self):
        item = draft(11)
        self.fixture(item)
        attempts = []
        async def lookup(codes):
            attempts.append(codes)
            def ean(code):
                digits = f'{int(code):012d}'
                return digits + str((-sum(int(number) * (1 if index % 2 == 0 else 3)
                    for index, number in enumerate(digits))) % 10)
            return SlogerBatchResult(items=[SlogerLookupResult(sku=code,
                observation=observation(code, eans=[ean(code)]) if code != '2' or len(attempts) > 2 else None,
                error='sloger_product_not_found' if code == '2' and len(attempts) <= 2 else None) for code in codes])
        with patch.object(source.sloger_data, 'lookup_products', side_effect=lookup), \
             patch.object(ai_content, 'create_batch', AsyncMock(side_effect=AssertionError('No AI for B2B data'))), \
             patch.object(product_import, 'public', AsyncMock(side_effect=lambda db, value: source.public_status(value))):
            await source.process(item.id)
            self.assertEqual([len(codes) for codes in attempts], [10, 1])
            self.assertEqual(source.public_status(item)['state'], 'failed')
            self.assertEqual(source.public_status(item)['completed'], 10)
            await source.retry(SimpleNamespace(flush=AsyncMock()), item, DraftRevision(expected_revision=item.revision))
            await source.process(item.id)
            self.assertEqual(attempts[-1], ['2'])
            self.assertEqual(item.status, 'draft')
            self.assertEqual(source.public_status(item)['completed'], 11)
            self.assertEqual(item.document['rows'][0]['values']['purchase_net'], '5.10')
            self.assertEqual(item.document['rows'][0]['values']['availability'], 'do 2-4 dní')
            self.assertFalse(any(row['values']['ai_enabled'] for row in item.document['rows']))

    async def test_duplicate_task_does_not_requery_completed_draft(self):
        item = draft()
        self.fixture(item)
        result = SlogerBatchResult(items=[SlogerLookupResult(sku='1', observation=observation())])
        with patch.object(source.sloger_data, 'lookup_products', AsyncMock(return_value=result)) as lookup:
            await source.process(item.id)
            await source.process(item.id)
            self.assertEqual(lookup.await_count, 1)

    async def test_contention_defers_an_unsent_batch_but_actual_failures_are_not_retried(self):
        result = SlogerBatchResult(items=[SlogerLookupResult(sku='1', observation=observation())])
        with patch.object(source.sloger_data, 'lookup_products', AsyncMock(side_effect=[
                CatalogError('sloger_b2b_busy', 'busy', 409), result])) as lookup, \
             patch.object(source.asyncio, 'sleep', AsyncMock()) as wait:
            self.assertEqual(await source._lookup(['1']), result)
            self.assertEqual(lookup.await_count, 2)
            wait.assert_awaited_once_with(2)
        with patch.object(source.sloger_data, 'lookup_products', AsyncMock(side_effect=
                CatalogError('sloger_b2b_unavailable', 'unavailable', 503))) as lookup:
            with self.assertRaises(CatalogError):
                await source._lookup(['1'])
            self.assertEqual(lookup.await_count, 1)

    async def test_canonical_resolution_sees_b2b_eans_and_preserves_existing_sku(self):
        item = draft()
        self.fixture(item)
        async def canonical(db, products):
            self.assertIn('0763477003706', products[0].eans)
            return [products[0].model_copy(update={'shop_code': 'EXISTING-9'})]
        result = SlogerBatchResult(items=[SlogerLookupResult(sku='1', observation=observation())])
        with patch.object(source.sloger_data, 'lookup_products', AsyncMock(return_value=result)), \
             patch.object(catalog_import, 'canonical_products', side_effect=canonical):
            await source.process(item.id)
        self.assertEqual(item.document['rows'][0]['values']['code'], 'EXISTING-9')
        self.assertEqual(item.status, 'draft')

    async def test_late_result_cannot_overwrite_a_retried_draft(self):
        item = draft()
        self.fixture(item)
        async def supersede(codes):
            item.document['source_lookup'] = {'state': 'queued', 'token': 'replacement', 'lease_expires_at': source._lease()}
            return SlogerBatchResult(items=[SlogerLookupResult(sku='1', observation=observation())])
        with patch.object(source.sloger_data, 'lookup_products', side_effect=supersede):
            await source.process(item.id)
        self.assertEqual(item.document['rows'][0]['source_status'], 'pending')
        self.assertEqual(item.document['source_lookup']['token'], 'replacement')

    async def test_explicit_feed_fallback_preserves_successes_and_reconciles_available_identity(self):
        item = draft(2)
        self.fixture(item)
        completed, failed = item.document['rows']
        source.apply_observation(completed, observation(), source.ShopImportOptions(), source.supplier_config('sloger'))
        completed['source']['supplier_observation_expires_at'] = (ai_content.now() + timedelta(hours=6)).isoformat()
        failed.update(source_status='failed', source_error='sloger_product_not_found')
        failed['values']['availability'] = 'overíme'
        item.document['source_lookup'] = {'state': 'failed', 'error': 'sloger_product_not_found'}
        original_completed, original_failed = deepcopy(completed), deepcopy(failed)
        async def canonical(db, products):
            self.assertIn('0763477003706', products[0].eans)
            return [products[0], products[1].model_copy(update={'shop_code': 'EXISTING-2'})]
        with patch.object(catalog_import, 'canonical_products', side_effect=canonical), \
             patch.object(product_import, 'public', AsyncMock(side_effect=lambda db, value: source.public_status(value))), \
             patch.object(source.sloger_data, 'lookup_products', AsyncMock(side_effect=AssertionError('No B2B on fallback'))), \
             patch.object(ai_content, 'create_batch', AsyncMock(side_effect=AssertionError('No AI on fallback'))):
            status = await source.skip(SimpleNamespace(flush=AsyncMock()), item, DraftRevision(expected_revision=1))
        self.assertEqual(status, {'state': 'skipped', 'total': 2, 'completed': 1, 'failed': 0, 'skipped': 1,
            'error': 'sloger_product_not_found', 'retryable': False, 'skippable': False})
        self.assertEqual(item.status, 'draft')
        self.assertEqual(item.revision, 2)
        completed, failed = item.document['rows']
        self.assertEqual(completed['values'], original_completed['values'])
        self.assertEqual(completed['source']['sloger_observation'], original_completed['source']['sloger_observation'])
        self.assertEqual(datetime.fromisoformat(completed['source']['supplier_observation_expires_at']),
            datetime.fromisoformat(original_completed['source']['supplier_observation_expires_at']))
        self.assertEqual(completed['source_status'], 'completed')
        self.assertEqual(failed['values'], {**original_failed['values'], 'code': 'EXISTING-2'})
        self.assertIsNone(failed['values']['purchase_net'])
        self.assertEqual(failed['source_error'], 'sloger_product_not_found')
        self.assertEqual(failed['canonical_code'], 'EXISTING-2')
        self.assertEqual(failed['source_status'], 'skipped')
        self.assertIn('sloger_b2b_feed_fallback', failed['warnings'])
        product_import.assert_editable(item)
        # Later cell validation retains the durable warning; completed rows still
        # carry their original expiry and normal publication freshness guards.
        product_import.validate_rows(item.document)
        self.assertIn('sloger_b2b_feed_fallback', failed['warnings'])
        with patch.object(source, '_freshness_seconds', AsyncMock(return_value=1)):
            with self.assertRaises(CatalogError):
                await source.assert_current_availability(object(), {
                    'supplier_availability_expires_at': completed['source']['supplier_observation_expires_at'],
                    'supplier_availability_observed_at': (ai_content.now() - timedelta(hours=1)).isoformat()})

    async def test_feed_fallback_requires_failed_source_and_current_revision(self):
        item = draft()
        self.fixture(item)
        db = SimpleNamespace(flush=AsyncMock())
        for state in ('queued', 'running', 'completed', 'skipped'):
            item.document['source_lookup'].update(state=state, lease_expires_at=source._lease())
            before = deepcopy(item.document)
            with self.assertRaises(CatalogError) as denied:
                await source.skip(db, item, DraftRevision(expected_revision=item.revision))
            self.assertEqual(denied.exception.code, 'import_source_not_skippable')
            self.assertEqual(item.document, before)
        item.document['source_lookup'] = {'state': 'failed', 'error': 'sloger_b2b_unavailable'}
        with self.assertRaises(CatalogError) as conflict:
            await source.skip(db, item, DraftRevision(expected_revision=item.revision + 1))
        self.assertEqual(conflict.exception.code, 'import_draft_changed')
        db.flush.assert_not_awaited()

    async def test_feed_fallback_cannot_unlock_saved_or_published_preparations(self):
        item = draft()
        self.fixture(item)
        item.document['source_lookup'] = {'state': 'failed', 'error': 'sloger_b2b_unavailable'}
        for status in ('saved', 'preview', 'publishing', 'published'):
            item.status = status
            with self.assertRaises(CatalogError):
                await source.skip(SimpleNamespace(flush=AsyncMock()), item, DraftRevision(expected_revision=1))
        item.status = 'source_failed'
        for key, value in (('saved_hash', 'saved'), ('publication', {'preview_id': 'old'}),
                ('publications', {'biketrek': {'preview_id': 'old'}}), ('previous_publications', ['old'])):
            item.document[key] = value
            with self.assertRaises(CatalogError):
                await source.skip(SimpleNamespace(flush=AsyncMock()), item, DraftRevision(expected_revision=1))
            self.assertEqual(item.document[key], value)
            del item.document[key]
        item.document['rows'][0]['hub_product_id'] = 42
        with self.assertRaises(CatalogError):
            await source.skip(SimpleNamespace(flush=AsyncMock()), item, DraftRevision(expected_revision=1))

    async def test_skipped_rows_do_not_inherit_unconfirmed_b2b_availability_or_quote(self):
        item = draft(2)
        self.fixture(item)
        for row in item.document['rows']:
            source.apply_observation(row, observation(row['values']['supplier_code']),
                source.ShopImportOptions(), source.supplier_config('sloger'))
            row['source'].update(supplier_stock='99', supplier_stock_external='999', supplier_available=True,
                supplier_observation_expires_at=(ai_content.now() + timedelta(hours=6)).isoformat())
            row.update(source_status='failed', source_error='sloger_b2b_unavailable')
        item.document['rows'][1]['manual_fields'] = ['availability', 'purchase_net']
        item.document['rows'][1]['values'].update(availability='na objednávku', purchase_net='12.00')
        item.document['source_lookup'] = {'state': 'failed', 'error': 'sloger_b2b_unavailable'}
        with patch.object(product_import, 'public', AsyncMock()):
            await source.skip(SimpleNamespace(flush=AsyncMock()), item, DraftRevision(expected_revision=1))
        derived, manual = item.document['rows']
        self.assertEqual(derived['values']['availability'], 'overíme')
        self.assertIsNone(derived['values']['purchase_net'])
        self.assertEqual(manual['values']['availability'], 'na objednávku')
        self.assertEqual(manual['values']['purchase_net'], '12.00')
        for row in item.document['rows']:
            for field in ('supplier_available', 'supplier_stock', 'supplier_stock_external',
                    'sloger_observation', 'supplier_observation_expires_at'):
                self.assertIsNone(row['source'][field])

    async def test_expired_worker_result_and_failure_cannot_overwrite_feed_fallback(self):
        item = draft()
        self.fixture(item)
        old_token = None
        async def skip_expired(codes):
            nonlocal old_token
            old_token = item.document['source_lookup']['token']
            item.document['source_lookup']['lease_expires_at'] = (ai_content.now() - timedelta(seconds=1)).isoformat()
            self.assertTrue(source.public_status(item)['skippable'])
            await source.skip(SimpleNamespace(flush=AsyncMock()), item, DraftRevision(expected_revision=item.revision))
            return SlogerBatchResult(items=[SlogerLookupResult(sku='1', observation=observation())])
        with patch.object(source.sloger_data, 'lookup_products', side_effect=skip_expired), \
             patch.object(product_import, 'public', AsyncMock(side_effect=lambda db, value: source.public_status(value))):
            await source.process(item.id)
            await source._fail(item.id, old_token, 'sloger_b2b_unavailable')
        self.assertEqual(item.document['source_lookup'], {'state': 'skipped', 'error': 'import_source_interrupted'})
        self.assertEqual(item.document['rows'][0]['source_status'], 'skipped')
        self.assertEqual(item.document['rows'][0]['source_error'], 'import_source_interrupted')
        self.assertIsNone(item.document['rows'][0]['values']['purchase_net'])
        self.assertEqual(item.status, 'draft')


if __name__ == '__main__':
    unittest.main()
