"""Private, bounded diagnostics from the actual read-only control snapshot."""
from copy import deepcopy
from hashlib import sha256
import json
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from inventory_hub.services import atomer_catalog_job_control as control
from inventory_hub.services import atomer_catalog_job_evidence as evidence
from inventory_hub.services import atomer_catalog_engine as engine
from inventory_hub.services import atomer_catalog_source_cards as local
from inventory_hub.services import atomer_source_state as source_state
import test_atomer_catalog_job_control as fixture


@unittest.skipUnless(fixture.fixture.TEST_URL, 'Requires dedicated localhost *_catalog_test database')
class JobEvidenceDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixture.JobControlDatabaseTests.asyncSetUp(self)
        # The compact engine fixture predates the complete progress projection.
        await self.conn.execute("UPDATE atomer_progress_items SET detail=detail || jsonb_build_object('kind',kind)")

    asyncTearDown = fixture.JobControlDatabaseTests.asyncTearDown

    async def snapshot(self):
        async with self.factory() as db:
            return await control._snapshot(db, self.campaign, 'v:P')

    async def detailed(self):
        async with self.factory() as db:
            return await control.get(db, self.campaign, 'v:P', include_evidence=True)

    async def test_default_contract_snapshot_hash_readonly_and_no_recovery(self):
        async with self.factory() as db:
            basic = await control.get(db, self.campaign, 'v:P')
        before = await fixture.JobControlDatabaseTests.protected(self)
        captured = await self.snapshot()
        with patch.object(engine, '_source', AsyncMock(side_effect=AssertionError('native/source I/O forbidden'))), \
             patch.object(engine, '_step', AsyncMock(side_effect=AssertionError('worker forbidden'))), \
             patch.object(local, '_receipts', AsyncMock(side_effect=AssertionError('receipt write forbidden'))), \
             patch.object(control, '_snapshot', AsyncMock(return_value=captured)) as snapshot:
            async with self.factory() as db:
                result = await control.get(db, self.campaign, 'v:P', include_evidence=True)
                snapshot.assert_awaited_once_with(db, self.campaign, 'v:P')
                self.assertEqual(await db.scalar(text('SHOW transaction_read_only')), 'on')
                self.assertEqual(await db.scalar(text('SHOW transaction_isolation')), 'repeatable read')
                with self.assertRaises(DBAPIError):
                    await db.execute(text("UPDATE atomer_catalog_jobs SET problem='forbidden'"))
        detail = result.pop('evidence')
        self.assertEqual(result, basic)
        self.assertEqual(detail['snapshot']['sha256'], basic['snapshot_hash'])
        encoded = json.dumps(captured, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self.assertEqual(detail['snapshot']['bytes'], len(encoded))
        self.assertEqual(detail['snapshot']['sha256'], sha256(encoded).hexdigest())
        self.assertTrue(detail['complete'], detail['problems'])
        self.assertEqual(detail['historical_native_effects'], 'not_determined')
        self.assertEqual(await fixture.JobControlDatabaseTests.protected(self), before)

    async def test_actual_source_receipt_writer_keeps_created_and_reused_history(self):
        state_rows = [dict(code='a', parent='P', kind='variant')]
        source_state.add_states([dict(code='P', source_visibility=True), dict(code='a', source_visibility=False)], state_rows, {})
        await self.conn.execute("UPDATE atomer_progress_items SET detail=detail || $1::jsonb WHERE code='a'",
                                json.dumps({'source_state': state_rows[0]['source_state']}))
        async with self.factory() as db:
            campaign = dict(await engine._one(db, 'SELECT * FROM atomer_catalog_campaigns'))
            first = dict(status='card_ready', action='created', created=True, card_id=31,
                         hub_sku='SUP-31', ean='8588000000010', gtins=['8588000000010'],
                         source_native_id='731', source_ean='8588000000010')
            await local._receipts(db, campaign, 'v:P', {'a': first})
            await local._receipts(db, campaign, 'v:P', {'a': {**first, 'action': 'reused', 'created': False}})
            await db.commit()
        result = await self.detailed()
        detail = result['evidence']
        self.assertFalse(result['eligible'])
        self.assertTrue(detail['complete'], detail['problems'])
        source = detail['captured']['source_cards'][0]['fields']
        self.assertIs(source['created'], True)
        values = source['evidence']['fields']
        self.assertEqual(values['action'], 'reused')
        self.assertIs(values['created'], False)
        self.assertEqual(values['creation_evidence']['fields']['action'], 'created')
        self.assertIs(values['creation_evidence']['fields']['created'], True)
        self.assertEqual(detail['captured']['proofs'][0]['fields']['state'], 'partial')
        self.assertTrue(any(r['kind'] == 'card_id' and r['value'] == 31 for r in detail['references']))
        raw = json.loads(await self.conn.fetchval('SELECT to_jsonb(t)::text FROM atomer_catalog_source_cards t'))
        self.assertEqual(detail['captured']['source_cards'][0]['canonical'], evidence.manifest(raw))
        self.assertEqual(detail['historical_native_effects'], 'not_determined')
        source = detail['captured']['rows']['a']['fields']['detail']['fields']['source_state']
        self.assertEqual((source['product_origin'], source['variant_origin']), ('buyers', 'buyers'))
        self.assertIs(source['product'], True)
        self.assertIs(source['variant'], False)

    async def test_saved_current_excluded_done_and_parent_scope_are_not_filtered(self):
        data = await self.snapshot()
        original = deepcopy(data)
        guards = data['job']['guards']
        guards['rows']['old'] = [7, 'done', 'xtrek']
        guards['scope']['old'] = 'xtrek'
        guards['excluded']['old'] = 'Historic manually excluded source leaf.'
        data['rows']['b'].update(revision=8, status='done', scope='biketrek')
        data['rows']['b']['detail'].update(product_id='902', variant_id='92', target_parent='new-parent')
        data['job']['receipt'] = dict(previous_guards=deepcopy(guards), previous_receipt=dict(
            product_id=901, variants={'old': 91}, cards={'old': dict(state='partial', evidence=dict(
                status='verified', card_id=31, xtrek=dict(product_id='901', variant_id='91',
                code='old', parent='old-parent', link_id=41)))}))
        previous = data['job']['receipt']['previous_guards']
        previous['rows']['older'] = [1, 'done', 'both']
        previous['scope']['older'] = 'both'
        previous['excluded']['older'] = 'Older untouched historical sibling.'
        detail = evidence.build(data)
        self.assertEqual(detail['scope']['saved_rows'], ['a', 'b', 'old'])
        self.assertEqual(detail['scope']['current_rows'], ['a', 'b'])
        self.assertEqual(detail['scope']['union'], ['a', 'b', 'old', 'older'])
        self.assertEqual(detail['source_allocation_query']['codes'], ['a', 'b'])
        self.assertEqual(detail['source_allocation_query']['missing_codes'], ['old', 'older'])
        self.assertFalse(detail['complete'])
        self.assertIn('allocation_query_does_not_cover_union', {p['code'] for p in detail['problems']})
        captured = detail['captured']
        self.assertEqual(captured['rows']['b']['fields']['status'], 'done')
        self.assertIn('old', captured['job']['fields']['guards']['fields']['excluded'])
        old = captured['job']['fields']['receipt']['fields']['previous_receipt']['fields']
        self.assertEqual(old['cards']['old']['fields']['evidence']['fields']['xtrek']['parent'], 'old-parent')
        self.assertEqual({str(r['value']) for r in detail['references'] if r['kind'] == 'native_id'}, {'901', '91', '902', '92'})
        self.assertEqual(original, await self.snapshot())

    async def test_identity_configuration_references_are_typed_not_silently_opaque(self):
        data = await self.snapshot()
        selection = data['campaign']['selection']
        selection['barcode_corrections'] = {'a': dict(before='', ean='8588000000010', parent='P',
            required_card_id=31, required_atomer_id='731', manufacturer_or_supplier='https://supplier.example.invalid/item',
            evidence='Synthetic private independently reviewed identity evidence.')}
        selection['zero_barcode_sentinels'] = {'b': '732'}
        result = evidence.build(data)
        self.assertTrue(result['complete'], result['problems'])
        self.assertTrue(any(r['kind'] == 'card_id' and r['value'] == 31 for r in result['references']))
        self.assertEqual({r['value'] for r in result['references'] if r['kind'] == 'source_id'}, {'731', '732'})
        self.assertNotIn('supplier.example.invalid', json.dumps(result))
        self.assertNotIn('independently reviewed', json.dumps(result))
        selection['barcode_corrections']['a']['new_identity'] = 'uninterpreted-secret'
        result = evidence.build(data)
        self.assertFalse(result['complete'])
        self.assertNotIn('uninterpreted-secret', json.dumps(result))

    async def test_unknown_malformed_and_private_values_never_become_a_clean_projection(self):
        data = await self.snapshot()
        secret = 'synthetic-private-token-and-contact@example.invalid'
        data['job']['problem'] = secret
        data['group']['note'] = secret
        data['rows']['a']['detail']['name'] = secret
        data['campaign']['actor'] = secret
        data['campaign']['options'] = {'arbitrary_configuration': secret}
        safe_looking_secret = 'synthetic_private_token_ABC123'
        data['job']['receipt'] = {'credentials': {'token': secret}, secret: 'hidden-key-value', safe_looking_secret: 1,
                                'dispatched': 'false', 'cards': []}
        before = deepcopy(data)
        detail = evidence.build(data)
        self.assertFalse(detail['complete'])
        rendered = json.dumps(detail)
        self.assertNotIn(secret, rendered)
        self.assertNotIn(safe_looking_secret, rendered)
        self.assertNotIn('hidden-key-value', rendered)
        self.assertIn('/job/receipt/@' + evidence.manifest('credentials')['sha256'], rendered)
        self.assertTrue(any(p['code'] == 'type_or_value_unverified' and p['path'] == '/job/receipt/dispatched'
                            for p in detail['problems']))
        self.assertEqual(detail['captured']['job']['canonical'], evidence.manifest(data['job']))
        self.assertEqual(data, before)
        data = await self.snapshot()
        data['rows']['a']['detail']['code'] = ''
        self.assertFalse(evidence.build(data)['complete'])

    async def test_sql_null_is_distinct_from_json_null_with_unchanged_default_guard(self):
        first = await self.detailed()
        await self.conn.execute("UPDATE atomer_catalog_jobs SET document='null'::jsonb WHERE family_key='v:P'")
        second = await self.detailed()
        self.assertIs(first['evidence']['captured']['document_is_sql_null'], True)
        self.assertIs(second['evidence']['captured']['document_is_sql_null'], False)
        self.assertIsNone(first['evidence']['captured']['job']['fields']['document'])
        self.assertIsNone(second['evidence']['captured']['job']['fields']['document'])
        self.assertNotEqual(first['snapshot_hash'], second['snapshot_hash'])
        async with self.factory() as db:
            basic = await control.get(db, self.campaign, 'v:P')
        self.assertEqual({k: v for k, v in second.items() if k != 'evidence'}, basic)

    async def test_sentinel_input_output_and_union_limits_are_explicit(self):
        base = await self.snapshot()
        data = deepcopy(base)
        row = dict(campaign=self.campaign, family_key='v:P', code='a', state='blocked',
                   evidence={'reason': 'Synthetic blocked source.'}, checked_at=data['job']['updated_at'])
        data['proofs'] = [row] * 501
        detail = evidence.build(data)
        self.assertFalse(detail['coverage']['proofs']['complete'])
        self.assertEqual(detail['coverage']['proofs']['query_limit'], 501)
        self.assertIn('query_sentinel_reached', {p['code'] for p in detail['problems']})
        self.assertFalse(detail['complete'])

        data = deepcopy(base)
        data['job']['problem'] = 'x' * evidence.MAX_BYTES
        detail = evidence.build(data)
        self.assertEqual(detail['problems'][0]['code'], 'snapshot_byte_limit')
        self.assertNotIn('captured', detail)
        # Repeated per-record manifests expand a small source snapshot. An
        # output limit must remove the projection and its complete flag.
        with patch.object(evidence, 'MAX_BYTES', evidence.manifest(base)['bytes'] + 1):
            detail = evidence.build(base)
        self.assertFalse(detail['complete'])
        self.assertEqual(detail['problems'][0]['code'], 'projection_byte_limit')
        self.assertNotIn('captured', detail)
        data = deepcopy(base)
        data['job']['guards']['rows'] = {str(i): [0, 'open', 'default'] for i in range(500)}
        detail = evidence.build(data)
        self.assertIn('family_union_limit', {p['code'] for p in detail['problems']})
        self.assertFalse(detail['complete'])

    async def test_unknown_object_width_and_nodes_are_bounded_before_projection_expands(self):
        data = await self.snapshot()
        data['job']['receipt'] = {f'unknown_{i}': 'private' for i in range(501)}
        detail = evidence.build(data)
        receipt = detail['captured']['job']['fields']['receipt']['fields']
        self.assertEqual(receipt['reason'], 'object_width_limit')
        self.assertNotIn('unknown_500', json.dumps(detail))
        self.assertFalse(detail['complete'])
        p = evidence.Projection()
        with patch.object(evidence, 'MAX_NODES', 3):
            projected = p.project({'unknown_a': 1, 'unknown_b': 2, 'unknown_c': 3}, 'receipt', '/receipt')
        self.assertEqual(projected['reason'], 'structure_limit')
        self.assertEqual(p.nodes, 3)
        self.assertIn('structure_limit', {item['code'] for item in p.problems})

    async def test_native_intent_and_uninterpreted_content_hold_with_all_anchors(self):
        data = await self.snapshot()
        data['job']['receipt'] = dict(dispatched=True, content_intent=dict(status='ready',
            product_id=901, variants={'a': 91}, anchors=dict(product_id=901, product_code='P',
                variant_ids={'a': 91}, variant_codes={'a': 'old-a'}, allow_parent_write=False),
            payload=dict(code='P', variants=[dict(code='old-a', descriptions=[{'title': 'Private content.'}])]),
            before=dict(code='P', product_id=901, variants=[dict(code='old-a', variant_id=91)])))
        detail = evidence.build(data)
        self.assertFalse(detail['complete'])
        self.assertEqual(detail['review_state'], 'hold')
        intent = detail['captured']['job']['fields']['receipt']['fields']['content_intent']
        self.assertEqual(intent['fields']['anchors']['variant_codes'], {'a': 'old-a'})
        self.assertEqual(intent['fields']['before']['fields']['product_id'], 901)
        self.assertNotIn('Private content.', json.dumps(detail))
        self.assertIn('content_schema_not_interpreted', {p['code'] for p in detail['problems']})
        self.assertEqual(detail['historical_native_effects'], 'not_determined')
