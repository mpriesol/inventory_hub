"""Retained local allocations, complete current intents and terminal lifecycle."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from inventory_hub.database import get_session
from inventory_hub.routers import atomer_progress as routes
from inventory_hub.services import atomer_catalog_handoff as service
from inventory_hub.services import atomer_catalog_job_control as controls
from inventory_hub.services import atomer_catalog_control as campaigns
from inventory_hub.services import atomer_catalog_engine as engine
from inventory_hub.services import atomer_catalog_source_cards as local
from inventory_hub.services import product_dependency_scan as scan
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK
from inventory_hub.settings import settings
import test_product_consolidation_db as fixture
from test_atomer_catalog_engine import selection


def request(**changes):
    return dict(request_id=str(uuid4()), family_key='v:P', expected_snapshot_hash='c' * 64,
        scope_codes=['a', 'b'], purpose='identity_migration', acknowledge_preserved_local_effects=True,
        acknowledge_review_changes=True, reason='Retain allocations and hand off the complete family.',
        confirmed=True, **changes)


class HandoffContractTests(unittest.TestCase):
    def test_proof_limit_diagnostics_are_sanitized_and_do_not_change_admitted_hashes(self):
        proof = dict(version=service.VERSION, retained={'text': 'Never expose this private value. ' * 20},
                     identities={'tokens': ['žltá']}, current_writers={'manifest': 'a' * 64})
        self.assertEqual(service._proof_hash(proof), controls._digest(proof))
        for section in ('retained_proof', 'candidate_proof', 'final_proof'):
            with self.subTest(section=section), patch.object(controls, 'MAX_BYTES', 100):
                with self.assertRaises(service.scope_inventory.ScopeLimit) as raised:
                    service._proof_hash(proof, section=section)
                blocker = raised.exception.blocker
                self.assertEqual((blocker['reason'], blocker['section'], blocker['limit']), ('proof_bytes', section, 100))
                self.assertEqual(blocker['observed'], len(json.dumps(proof, ensure_ascii=False).encode()))
                self.assertEqual(blocker['encoding'], service.PROOF_ENCODING)
                self.assertEqual(set(blocker['section_sizes']), {'retained', 'identities', 'current_writers'})
                self.assertNotIn('Never expose', json.dumps(blocker))
                self.assertNotIn('žltá', json.dumps(blocker, ensure_ascii=False))

    def test_retained_sentinel_and_closure_set_limits_do_not_claim_complete_counts(self):
        data = dict(proofs=[{}] * 501, source_cards=[])
        own = dict(blockers=['catalog_handoff_scope_limit'], scope_codes=['a'])
        with self.assertRaises(service.scope_inventory.ScopeLimit) as raised:
            service._retained_limits(data, own)
        self.assertEqual(raised.exception.blocker, dict(code='catalog_handoff_scope_limit', reason='retained_rows',
            section='proofs', observed=501, limit=500, encoding='rows', count_is_exact=False))
        async def expand():
            with patch.object(service.scope_inventory, 'seed', return_value=(set(), {'token-' + str(i) for i in range(5001)}, set(), [], set())):
                await service.scope_inventory.closure(None, {}, [])
        with self.assertRaises(service.scope_inventory.ScopeLimit) as raised:
            asyncio.run(expand())
        self.assertEqual(raised.exception.blocker['reason'], 'closure_set')
        self.assertEqual((raised.exception.blocker['section'], raised.exception.blocker['observed'],
                          raised.exception.blocker['iteration']), ('tokens', 5001, 1))
        self.assertTrue(raised.exception.blocker['count_is_exact'])

    def test_explicit_full_scope_and_acknowledgements(self):
        body = request()
        self.assertEqual(service.HandoffRequest(**body).scope_codes, ['a', 'b'])
        for changes in ({'scope_codes': []}, {'scope_codes': ['a', 'a']}, {'scope_codes': ['a'] * 501},
                        {'confirmed': 1}, {'confirmed': 'true'}, {'acknowledge_preserved_local_effects': False},
                        {'acknowledge_review_changes': 1}, {'purpose': 'completed'}, {'ignore_writers': True},
                        {'reason': 'short'}, {'reason': 'A reason\nwith a newline'}, {'expected_snapshot_hash': 'x'}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                service.HandoffRequest(**{**body, **changes})

    def test_routes_require_operator_are_no_store_and_use_dedicated_schema(self):
        db_calls = []
        async def session():
            db_calls.append(True)
            yield SimpleNamespace()
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[get_session] = session
        base = '/migration/atomer-progress/catalog-engine/campaigns/' + 'a' * 64 + '/jobs/'
        token = 'synthetic-handoff-' + 'x' * 32
        with patch.object(settings, 'AI_CONTENT_ACCESS_TOKEN', SecretStr(token)), TestClient(app) as client:
            for method, path, body, name in [('GET', base + 'handoff-control?family=v:P', None, 'get'),
                                           ('POST', base + 'handoff', request(), 'change')]:
                with patch.object(service, name, AsyncMock(return_value={'status': 'completed'})) as operation:
                    count = len(db_calls)
                    denied = client.request(method, path, json=body)
                    self.assertEqual((denied.status_code, denied.headers['cache-control']), (401, 'no-store'))
                    self.assertEqual(len(db_calls), count)
                    operation.assert_not_awaited()
                    response = client.request(method, path, json=body, headers={'Authorization': 'Bearer ' + token})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.headers['cache-control'], 'no-store')
                    if name == 'change':
                        self.assertIsInstance(operation.await_args.args[2], service.HandoffRequest)
                        self.assertEqual(operation.await_args.args[-1], 'operator-token')
            invalid = client.post(base + 'handoff', json={**request(), 'scope_codes': ['a', 'a']},
                headers={'Authorization': 'Bearer ' + token})
            self.assertEqual((invalid.status_code, invalid.headers['cache-control']), (422, 'no-store'))

    def test_get_limit_is_a_nonapplicable_nostore_diagnostic_and_post_remains_conflict(self):
        db = SimpleNamespace(execute=AsyncMock(), rollback=AsyncMock())
        async def session():
            yield db
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[get_session] = session
        base = '/migration/atomer-progress/catalog-engine/campaigns/' + 'a' * 64 + '/jobs/'
        token = 'synthetic-limit-' + 'x' * 32
        headers = {'Authorization': 'Bearer ' + token}
        limit = service.scope_inventory.ScopeLimit('selection_rows', 'mappings', 6001, 5000, encoding='rows', iteration=3)
        with patch.object(settings, 'AI_CONTENT_ACCESS_TOKEN', SecretStr(token)), TestClient(app) as client:
            with patch.object(service.scope_inventory, 'schema', AsyncMock(return_value={})), \
                    patch.object(service, '_capture', AsyncMock(side_effect=limit)):
                response = client.get(base + 'handoff-control?family=v:P', headers=headers)
            self.assertEqual((response.status_code, response.headers['cache-control']), (200, 'no-store'))
            result = response.json()
            self.assertFalse(result['eligible'])
            self.assertFalse(result['complete'])
            self.assertIsNone(result['snapshot_hash'])
            self.assertEqual(result['blockers'], [limit.blocker])
            db.rollback.assert_awaited_once()
            self.assertIn('REPEATABLE READ READ ONLY', str(db.execute.await_args.args[0]))
            with patch.object(service, 'change', AsyncMock(side_effect=limit)):
                response = client.post(base + 'handoff', json=request(), headers=headers)
            self.assertEqual((response.status_code, response.headers['cache-control']), (409, 'no-store'))
            self.assertEqual(response.json(), {'detail': {'code': 'catalog_handoff_scope_limit'}})


@unittest.skipUnless(fixture.TEST_URL, 'Requires dedicated localhost *_catalog_test database')
class HandoffDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Composition reuses the full installed schema and stock fixture without
        # inheriting/rerunning unrelated consolidation tests under a new name.
        self.base = fixture.ProductConsolidationDatabaseTests()
        await self.base.asyncSetUp()
        self.sessions = self.base.sessions
        self.campaign = engine._campaign_id(selection())
        self.no_http = patch('requests.sessions.Session.request', side_effect=AssertionError('native HTTP forbidden'))
        self.no_http.start()
        self.fingerprint = patch.object(engine.repair, 'target_fingerprint', return_value='x' * 64)
        self.fingerprint.start()
        async with self.base.transaction() as db:
            await db.execute(text("UPDATE shop_products SET external_product_id=CASE product_id WHEN :id THEN '9001' ELSE '9002' END"),
                {'id': self.base.keeper})
            await db.execute(text("UPDATE products SET created_from_source='atomer' WHERE id=:id"), {'id': self.base.retired})
            await db.execute(text("""INSERT INTO atomer_progress_sources(sha256,source_rows,identities)
                VALUES(repeat('a',64),2,'[]');"""))
            await db.execute(text("INSERT INTO atomer_review_exports(sha256,format,compressed) VALUES(repeat('b',64),'admin_csv','')"))
            await db.execute(text("""INSERT INTO atomer_progress_runs(source_sha256,admin_export_sha256,checked_at,totals)
                VALUES(repeat('a',64),repeat('b',64),now(),'{}')"""))
            for code in ('a', 'b'):
                await db.execute(text("""INSERT INTO atomer_progress_items(run_id,code,kind,state,family_key,detail)
                    VALUES(1,:code,'variant','needs_both','v:P',CAST(:detail AS jsonb))"""),
                    dict(code=code, detail=json.dumps(dict(code=code, kind='variant', state='needs_both',
                        card_state='missing', reasons=[], bike_state='no_card', ean=fixture.EAN))))
            await engine.process(db, selection(), 'apply', 'operator')
            campaign = dict(await engine._one(db, 'SELECT * FROM atomer_catalog_campaigns'))
            job = dict(await engine._one(db, 'SELECT * FROM atomer_catalog_jobs'))
            rows, group = await engine._family(db, 1, 'v:P')
            guards = await engine._claim(db, campaign, job, rows, group)
            await engine._job(db, self.campaign, 'v:P', 'pending', guards=guards)
            ready = {code: dict(status='card_ready', action='created' if code == 'b' else 'reused', created=code == 'b',
                card_id=self.base.retired if code == 'b' else self.base.keeper,
                hub_sku='SYN-OLD' if code == 'b' else 'SYN-KEEP', ean=fixture.EAN, gtins=[fixture.EAN],
                source_native_id='102' if code == 'b' else '101', source_ean=fixture.EAN) for code in ('a', 'b')}
            await local._receipts(db, campaign, 'v:P', ready)
            await db.execute(text("""UPDATE atomer_catalog_jobs SET attempts=6,source_card_stage='checked',
                problem='synthetic preflight unavailable',source_card_attempts=2"""))
        await self.campaign_action('pause')

    async def asyncTearDown(self):
        self.fingerprint.stop()
        self.no_http.stop()
        await self.base.asyncTearDown()

    async def campaign_action(self, action):
        async with self.sessions() as db:
            current = await campaigns.get(db, self.campaign)
            payload = campaigns.ControlRequest(request_id=uuid4(), expected_revision=current['revision'],
                expected_updated_at=current['updated_at'], reason='Synthetic campaign lifecycle boundary.', confirmed=True)
            return await campaigns.change(db, self.campaign, action, payload, 'operator')

    async def current(self):
        async with self.sessions() as db:
            return await service.get(db, self.campaign, 'v:P')

    async def payload(self):
        current = await self.current()
        self.assertTrue(current['eligible'], current)
        return service.HandoffRequest(**{**request(), 'expected_snapshot_hash': current['snapshot_hash'],
            'scope_codes': current['scope_codes']})

    async def change(self, payload=None):
        payload = payload or await self.payload()
        async with self.sessions() as db:
            return await service.change(db, self.campaign, payload, 'user:7')

    async def protected(self):
        tables = ('products', 'product_identifiers', 'product_sku_identities', 'shop_products',
            'stock_balances', 'stock_movements', 'fifo_layers', 'atomer_catalog_proofs', 'atomer_catalog_source_cards',
            'atomer_review_work', 'atomer_review_events')
        async with self.sessions() as db:
            return {table: await db.scalar(text(f"""SELECT encode(sha256(convert_to(coalesce(
                string_agg(to_jsonb(t)::text,E'\\n' ORDER BY to_jsonb(t)::text),''),'UTF8')),'hex') FROM {table} t"""))
                for table in tables}

    async def test_full_mapping_census_returns_exact_readonly_limit_and_post_rolls_back(self):
        payload = await self.payload()
        async with self.base.transaction() as db:
            await db.execute(text("""WITH added AS (
                INSERT INTO products(sku,name) SELECT 'synthetic-sibling-'||n,'Synthetic sibling'
                FROM generate_series(1,5005) n RETURNING id,sku)
                INSERT INTO shop_products(shop_id,product_id,external_product_id,
                external_variant_id,external_code,parent_code,is_variant)
                SELECT :s,id,'9001',(10000+id)::text,sku,'synthetic-parent',true FROM added"""),
                {'s': self.base.shop_id})
        protected = await self.protected()
        async with self.sessions() as db:
            original_job = await db.scalar(text('SELECT to_jsonb(t) FROM atomer_catalog_jobs t'))
        current = await self.current()
        self.assertFalse(current['eligible'])
        self.assertFalse(current['complete'])
        self.assertIsNone(current['snapshot_hash'])
        self.assertEqual(current['blockers'], [dict(code='catalog_handoff_scope_limit', reason='selection_rows',
            section='mappings', observed=5007, limit=5000, encoding='rows', count_is_exact=True, iteration=2)])
        self.assertEqual(current['remote_requests'], 0)
        self.assertFalse(current['remote_write_performed'])
        self.assertEqual(current['historical_native_effects'], 'not_determined')
        # A separate row census must not materialize any over-bound payload.
        async with self.sessions() as db:
            with patch.object(db, 'execute', side_effect=AssertionError('over-bound body must not be fetched')):
                with self.assertRaises(service.scope_inventory.ScopeLimit) as raised:
                    await service.scope_inventory.rows(db, 'SELECT n FROM generate_series(1,9283) n', section='synthetic_census')
                self.assertEqual(raised.exception.blocker['observed'], 9283)
        with self.assertRaises(service.scope_inventory.ScopeLimit):
            await self.change(payload)
        self.assertEqual(await self.protected(), protected)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(text('SELECT to_jsonb(t) FROM atomer_catalog_jobs t')), original_job)
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM atomer_catalog_job_controls')), 0)
            self.assertTrue(await db.scalar(text('SELECT pg_try_advisory_xact_lock(:k)'), {'k': IDENTITY_WRITE_LOCK}))

    async def test_full_product_body_byte_limit_is_sanitized_without_confirmation_hash(self):
        marker = 'PRIVATE_SYNTHETIC_FEED_BODY_DO_NOT_EXPOSE'
        async with self.base.transaction() as db:
            await db.execute(text('UPDATE products SET supplier_feed_data=CAST(:d AS jsonb) WHERE id=:id'),
                {'id': self.base.keeper, 'd': json.dumps({'description': marker * 500})})
        payload = await self.payload()
        protected = await self.protected()
        with patch.object(service.scope_inventory, 'MAX_BYTES', 10000):
            current = await self.current()
            self.assertIsNone(current['snapshot_hash'])
            self.assertFalse(current['eligible'])
            self.assertFalse(current['complete'])
            blocker = current['blockers'][0]
            self.assertEqual((blocker['reason'], blocker['section'], blocker['iteration']), ('selection_bytes', 'products', 1))
            self.assertGreater(blocker['observed'], blocker['limit'])
            self.assertGreater(blocker['largest_row_bytes'], 10000)
            self.assertEqual(blocker['encoding'], service.scope_inventory.ROW_ENCODING)
            self.assertNotIn(marker, json.dumps(current))
            with self.assertRaises(service.scope_inventory.ScopeLimit):
                await self.change(payload)
        self.assertEqual(await self.protected(), protected)
        self.assertEqual((await self.current())['snapshot_hash'], payload.expected_snapshot_hash)

    async def test_iteration_and_combined_identity_limits_are_distinct_readonly_holds(self):
        before = await self.current()
        protected = await self.protected()
        with patch.object(service.scope_inventory, 'MAX_ITERATIONS', 1):
            current = await self.current()
        self.assertIsNone(current['snapshot_hash'])
        self.assertEqual(current['blockers'][0]['reason'], 'closure_iterations')
        self.assertEqual(current['blockers'][0]['section'], 'fixed_point')
        self.assertEqual(current['blockers'][0]['iteration'], 1)
        async with self.sessions() as db:
            data = await controls._snapshot(db, self.campaign, 'v:P')
            _, identity = await service.scope_inventory.closure(db, data, before['scope_codes'])
        body_max = max(len(json.dumps([{'data': row} for row in identity[key]]).encode())
                       for key in ('products', 'identifiers', 'sku_identities', 'mappings', 'aliases'))
        self.assertGreater(len(json.dumps(identity).encode()), body_max + 1)
        with patch.object(service.scope_inventory, 'MAX_BYTES', body_max + 1):
            current = await self.current()
        self.assertIsNone(current['snapshot_hash'])
        self.assertFalse(current['eligible'])
        self.assertEqual(current['blockers'][0]['reason'], 'identity_snapshot_bytes')
        self.assertEqual(set(current['blockers'][0]['section_sizes']), set(identity))
        self.assertEqual(await self.protected(), protected)
        self.assertEqual((await self.current())['snapshot_hash'], before['snapshot_hash'])

    async def test_retained_local_effects_repeatable_read_exact_replay_and_immutable_history(self):
        protected = await self.protected()
        before = await self.current()
        self.assertTrue(before['eligible'], before)
        self.assertEqual(before['snapshot_hash'], (await self.current())['snapshot_hash'])
        self.assertEqual(before['local_effects'], dict(source_rows=2, proof_rows=2, created=1, reused=1, blocked=0))
        payload = await self.payload()
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM atomer_catalog_job_controls')), 0)
            original = await db.scalar(text('SELECT to_jsonb(t) FROM atomer_catalog_jobs t'))
        result = await self.change(payload)
        self.assertEqual(result, await self.change(payload))
        self.assertFalse(result['source_publication_completed'])
        self.assertFalse(result['content_publication_completed'])
        self.assertEqual(result['historical_native_effects'], 'not_determined')
        self.assertEqual(await self.protected(), protected)
        async with self.sessions() as db:
            self.assertEqual(await controls.receipt(db, payload.request_id), result)
            self.assertEqual(await db.scalar(text('SELECT before_job FROM atomer_catalog_job_controls')), original)
            job = await db.scalar(text('SELECT to_jsonb(t) FROM atomer_catalog_jobs t'))
            for key in controls.JOB_FIELDS - {'stage', 'source_card_stage', 'operator_withdrawn',
                    'operator_control_revision', 'operator_control_id', 'updated_at'}:
                self.assertEqual(job[key], original[key], key)
            self.assertTrue(await db.scalar(text('SELECT operator_paused FROM atomer_catalog_campaigns')))
        # Use the ordinary dependency scanner with NO candidate exemption after
        # handoff. Retained proofs and source cards must not revive the job as
        # an active writer for a subsequent independent consolidation.
        async with self.sessions() as db:
            data = await controls._snapshot(db, self.campaign, 'v:P')
            scope, _ = await service.scope_inventory.closure(db, data, before['scope_codes'])
            typed = await scan.inspect(db, scope=scope, columns=await service.scope_inventory.schema(db),
                open_condition=service.scope_inventory.dependencies._open_condition, request_id=None)
            self.assertTrue(typed['complete'], typed)
            self.assertEqual(typed['tables']['atomer_catalog_jobs']['count'], 0)
            self.assertFalse(any(b.get('table') == 'atomer_catalog_jobs' for b in typed['blockers']))
        with self.assertRaisesRegex(engine.ReviewError, 'request_conflict'):
            await self.change(payload.model_copy(update={'reason': 'A different payload under the same UUID.'}))

    async def test_no_current_intent_claim_from_empty_projection_or_malformed_local_producer(self):
        async with self.sessions() as db:
            data = await controls._snapshot(db, self.campaign, 'v:P')
        self.assertFalse(service.inspect(data)['blockers'])
        for change in ({'stage': 'prepared'}, {'document': {}}, {'receipt': {'dispatched': False}},
                       {'receipt': {'previous_document': None}}, {'operator_control_revision': 1}, {'attempts': -1}):
            bad = deepcopy(data); bad['job'].update(change)
            with self.subTest(change=change):
                self.assertTrue(service.inspect(bad)['blockers'])
        for key in service.READY_FIELDS:
            bad = deepcopy(data); bad['source_cards'][0]['evidence'].pop(key)
            with self.subTest(missing=key):
                self.assertTrue(service.inspect(bad)['blockers'])
        for value in ('identity_card_and_mapped_content', 'identity_and_card'):
            bad = deepcopy(data); bad['source_cards'][0]['evidence']['verification_scope'] = value
            self.assertTrue(service.inspect(bad)['blockers'])
        for value in (None, [], 'unknown', {'status': 'card_ready', 'creation_evidence': 'unknown'}):
            bad = deepcopy(data); bad['source_cards'][0]['evidence'] = value
            self.assertTrue(service.inspect(bad)['blockers'])
        bad = deepcopy(data); bad['proofs'][0]['state'] = 'blocked'
        self.assertTrue(service.inspect(bad)['blockers'])
        async with self.base.transaction() as db:
            await db.execute(text("UPDATE atomer_catalog_jobs SET document='null'::jsonb"))
        current = await self.current()
        self.assertFalse(current['eligible'])
        self.assertIn('catalog_handoff_current_intent_unverified', [b['code'] for b in current['blockers']])

    async def test_complete_saved_current_proof_union_review_drift_and_large_campaign_corrections(self):
        async with self.base.transaction() as db:
            await db.execute(text("""INSERT INTO atomer_catalog_proofs(campaign,family_key,code,state,evidence)
                VALUES(:c,'v:P','old-excluded-sibling','blocked','{"reason":"Manual source review."}')"""), {'c': self.campaign})
            await db.execute(text("""UPDATE atomer_review_work SET revision=revision+1,status='done',scope='xtrek'
                WHERE kind='item' AND key='a'"""))
            correction = {f'other-{i}': {'before': '', 'ean': fixture.EAN, 'manufacturer_or_supplier': 'Synthetic',
                'evidence': 'Synthetic source.'} for i in range(1155)}
            await db.execute(text("UPDATE atomer_catalog_campaigns SET selection=jsonb_set(selection,'{barcode_corrections}',CAST(:c AS jsonb))"),
                {'c': json.dumps(correction)})
        current = await self.current()
        self.assertTrue(current['eligible'], current)
        self.assertTrue(current['review_changed'])
        self.assertEqual(current['scope_codes'], ['a', 'b', 'old-excluded-sibling'])
        incomplete = service.HandoffRequest(**{**request(), 'expected_snapshot_hash': current['snapshot_hash']})
        with self.assertRaisesRegex(engine.ReviewError, 'catalog_handoff_changed'):
            await self.change(incomplete)
        result = await self.change()
        self.assertEqual(result['scope_codes'], current['scope_codes'])
        self.assertEqual(result['local_effects']['proof_rows'], 3)

    async def test_fresh_hash_rejects_identity_change_and_missing_native_anchor_is_explicit_hold(self):
        payload = await self.payload()
        async with self.base.transaction() as db:
            await db.execute(text("UPDATE products SET name='Changed synthetic title' WHERE id=:id"), {'id': self.base.keeper})
        with self.assertRaisesRegex(engine.ReviewError, 'catalog_handoff_changed'):
            await self.change(payload)
        async with self.base.transaction() as db:
            await db.execute(text('UPDATE shop_products SET external_product_id=NULL WHERE id=:id'), {'id': self.base.mapping_ids[0]})
        current = await self.current()
        self.assertFalse(current['eligible'])
        self.assertEqual(current['blockers'], [dict(code='catalog_handoff_native_identity_unverified',
            mapping_id=self.base.mapping_ids[0], shop_code='biketrek')])

    async def test_renamed_native_repair_and_unknown_json_writer_hold_without_product_fk(self):
        async with self.base.transaction() as db:
            await db.execute(text("""INSERT INTO atomer_variant_repairs(id,parent_code,state,document)
                VALUES(:id,'different-name','uncertain',CAST(:d AS jsonb))"""),
                {'id': 'f' * 64, 'd': json.dumps({'identity': {'product_id': 9001, 'variant_id': 42}})})
        current = await self.current()
        self.assertFalse(current['eligible'])
        self.assertTrue(any(b.get('table') == 'json:atomer_variant_repairs' for b in current['blockers']))
        async with self.base.transaction() as db:
            await db.execute(text("UPDATE atomer_variant_repairs SET state='verified'"))
        self.assertTrue((await self.current())['eligible'])
        async with self.base.transaction() as db:
            await db.execute(text("UPDATE atomer_variant_repairs SET document=document||CAST(:d AS jsonb)"),
                {'d': json.dumps({'content_intent': {'state': 'uncertain'}})})
        self.assertFalse((await self.current())['eligible'])
        async with self.base.transaction() as db:
            await db.execute(text("UPDATE atomer_variant_repairs SET document=document-'content_intent'"))
        async with self.base.transaction() as db:
            await db.execute(text('CREATE TABLE synthetic_future_writer(id bigint PRIMARY KEY,status text,state text,document jsonb)'))
            await db.execute(text("INSERT INTO synthetic_future_writer VALUES(1,'completed','sending',CAST(:d AS jsonb))"),
                {'d': json.dumps({'unrecognized_numeric_target': self.base.retired})})
        current = await self.current()
        self.assertFalse(current['eligible'])
        self.assertTrue(any(b.get('table') == 'json:synthetic_future_writer' for b in current['blockers']))
        async with self.base.transaction() as db:
            await db.execute(text('ALTER TABLE synthetic_future_writer ALTER COLUMN document TYPE json USING document::json'))
        self.assertFalse((await self.current())['eligible'])

    async def test_real_installed_writer_schema_and_bounded_incomplete_inventory(self):
        async with self.sessions() as db:
            columns = await service.scope_inventory.schema(db)
        for table, expected in service.scope_inventory.WRITER_JSON.items():
            self.assertEqual({k for k, v in columns[table].items() if v == 'jsonb'}, expected, table)
        async with self.base.transaction() as db:
            run_id = str(uuid4())
            await db.execute(text("""INSERT INTO stock_sync_runs(id,shop_id,warehouse_id,target_fingerprint,
                settings_revision,trigger,status) VALUES(:id,:s,:w,repeat('a',64),1,'manual','completed')"""),
                {'id': run_id, 's': self.base.shop_id, 'w': self.base.warehouse_id})
            await db.execute(text("""INSERT INTO stock_sync_items(run_id,shop_id,warehouse_id,sku,status,delta_snapshot)
                VALUES(:id,:s,:w,'SYN-KEEP','verified',CAST(:d AS jsonb))"""),
                {'id': run_id, 's': self.base.shop_id, 'w': self.base.warehouse_id,
                 'd': json.dumps({'product_id': self.base.keeper})})
            await db.execute(text('CREATE TABLE synthetic_many_writer(id integer PRIMARY KEY,document jsonb)'))
            await db.execute(text("INSERT INTO synthetic_many_writer SELECT i,'{}'::jsonb FROM generate_series(1,5001) i"))
        current = await self.current()
        self.assertFalse(current['eligible'])
        self.assertFalse(current['complete'])
        facts = current['writer_inventory']['facts']
        self.assertEqual(facts['json:stock_sync_items']['count'], 0)
        self.assertEqual(facts['json:synthetic_many_writer'], {'count': 5001, 'hash': None, 'complete': False})

    async def test_actual_source_reuse_then_block_preserves_sticky_creation_and_all_local_history(self):
        async with self.base.transaction() as db:
            campaign = dict(await engine._one(db, 'SELECT * FROM atomer_catalog_campaigns'))
            original = await db.scalar(text("SELECT evidence FROM atomer_catalog_source_cards WHERE code='b'"))
            reused = {**original, 'action': 'reused', 'created': False}
            await local._receipts(db, campaign, 'v:P', {'b': reused})
        self.assertTrue((await self.current())['eligible'])
        async with self.base.transaction() as db:
            campaign = dict(await engine._one(db, 'SELECT * FROM atomer_catalog_campaigns'))
            await local._receipts(db, campaign, 'v:P', {'b': dict(status='blocked', reason='local_mapping_conflict',
                card_id=None, hub_sku=None, xtrek=None, biketrek=None)})
        current = await self.current()
        self.assertTrue(current['eligible'], current)
        self.assertEqual((current['local_effects']['created'], current['local_effects']['blocked']), (1, 1))
        before = await self.protected()
        await self.change()
        self.assertEqual(await self.protected(), before)

    async def test_unrecognized_compound_separator_holds_and_native_target_names_are_retained(self):
        async with self.base.transaction() as db:
            await db.execute(text("UPDATE atomer_progress_items SET detail=detail||CAST(:d AS jsonb) WHERE code='a'"),
                {'d': json.dumps({'ean': fixture.EAN + '|' + fixture.OTHER_EAN})})
        current = await self.current()
        self.assertFalse(current['eligible'])
        self.assertEqual(current['blockers'][0]['code'], 'catalog_handoff_barcode_scope_unverified')
        async with self.base.transaction() as db:
            await db.execute(text("UPDATE atomer_progress_items SET detail=detail||CAST(:d AS jsonb) WHERE code='a'"),
                {'d': json.dumps({'ean': fixture.EAN + '/' + fixture.OTHER_EAN,
                    'target_code': 'unmapped-renamed-native', 'target_parent': 'renamed-native-parent'})})
            await db.execute(text("""INSERT INTO atomer_variant_repairs(id,parent_code,state,document)
                VALUES(:id,'renamed-native-parent','sending','{}')"""), {'id': 'e' * 64})
        current = await self.current()
        self.assertFalse(current['eligible'])
        self.assertTrue(any(b.get('table') == 'json:atomer_variant_repairs' for b in current['blockers']))

    async def test_retired_source_receipt_expands_current_canonical_owner_with_hidden_mapping(self):
        async with self.base.transaction() as db:
            former = await db.scalar(text("INSERT INTO products(sku,name,is_active) VALUES('OLD-SOURCE','Former synthetic card',false) RETURNING id"))
            keeper = await db.scalar(text("INSERT INTO products(sku,name) VALUES('CURRENT-CANON','Current synthetic card') RETURNING id"))
            await db.execute(text("""INSERT INTO shop_products(shop_id,product_id,external_product_id,external_code,is_variant,is_listed)
                VALUES(:s,:p,'9100','native-canonical',false,false)"""), {'s': self.base.shop_id, 'p': keeper})
            doc = {'request': {'mode': 'identity_only_preserve_unconfirmed_history', 'same_physical_retail_unit_confirmed': True,
                'keeper_product_id': keeper, 'retired_product_ids': [former]}}
            identifier = str(uuid4())
            await db.execute(text("""INSERT INTO product_consolidations(id,request_hash,preview_hash,document,status,expires_at,completed_at,result)
                VALUES(:id,repeat('1',64),repeat('2',64),CAST(:d AS jsonb),'completed',now(),now(),'{}')"""),
                {'id': identifier, 'd': json.dumps(doc)})
            await db.execute(text("INSERT INTO product_canonical_aliases(retired_product_id,canonical_product_id,consolidation_id) VALUES(:r,:k,:c)"),
                {'r': former, 'k': keeper, 'c': identifier})
            await db.execute(text("""UPDATE atomer_catalog_source_cards SET card_id=:id,
                evidence=evidence||CAST(:e AS jsonb) WHERE code='a'"""),
                {'id': former, 'e': json.dumps({'card_id': former, 'hub_sku': 'OLD-SOURCE'})})
            await db.execute(text("UPDATE atomer_catalog_proofs SET evidence=evidence||CAST(:e AS jsonb) WHERE code='a'"),
                {'e': json.dumps({'card_id': former, 'hub_sku': 'OLD-SOURCE'})})
            await db.execute(text("""INSERT INTO product_editor_publications(id,product_id,shop_id,state,document)
                VALUES(:id,:p,:s,'sending','{}')"""), {'id': str(uuid4()), 'p': keeper, 's': self.base.shop_id})
        current = await self.current()
        self.assertIn(former, current['retained_card_ids'])
        self.assertIn(keeper, current['retained_card_ids'])
        self.assertEqual(current['mapping_count'], 3)
        self.assertFalse(current['eligible'])
        self.assertTrue(any(b.get('table') == 'fk:product_editor_publications.product_id' for b in current['blockers']))

    async def test_renamed_allocation_and_exact_saved_mapping_id_are_not_lost(self):
        async with self.base.transaction() as db:
            allocation = await db.scalar(text("""INSERT INTO products(sku,name,created_from_source)
                VALUES('RENAMED-ALLOCATION','Renamed source allocation','atomer') RETURNING id"""))
            save_id = str(uuid4())
            await db.execute(text("INSERT INTO product_editor_saves(request_id,input_hash,result) VALUES(:id,repeat('a',64),'{}')"),
                {'id': save_id})
            await db.execute(text("""INSERT INTO product_editor_audit(request_id,product_id,revision,before_data,after_data)
                VALUES(:id,:p,1,CAST(:before AS jsonb),CAST(:after AS jsonb))"""),
                {'id': save_id, 'p': allocation, 'before': json.dumps({'sku': 'A'}),
                 'after': json.dumps({'sku': 'RENAMED-ALLOCATION'})})
            owner = await db.scalar(text("INSERT INTO products(sku,name) VALUES('OTHER-OWNER','Other current owner') RETURNING id"))
            mapping = await db.scalar(text("""INSERT INTO shop_products(shop_id,product_id,external_product_id,external_code,is_variant)
                VALUES(:s,:p,'9200','current-native-code',false) RETURNING id"""), {'s': self.base.shop_id, 'p': owner})
            await db.execute(text("UPDATE atomer_progress_items SET detail=detail||CAST(:d AS jsonb) WHERE code='a'"),
                {'d': json.dumps({'bike': dict(product_id=None, variant_id=None, code='old-native-name', parent='',
                    link_id=mapping, is_variant=False, reason='Retained mapping reference.')})})
            await db.execute(text("""INSERT INTO product_editor_publications(id,product_id,shop_id,state,document)
                VALUES(:id,:p,:s,'uncertain','{}')"""), {'id': str(uuid4()), 'p': owner, 's': self.base.shop_id})
        before = await self.protected()
        current = await self.current()
        self.assertIn(allocation, current['retained_card_ids'])
        self.assertIn(owner, current['retained_card_ids'])
        self.assertFalse(current['eligible'])
        self.assertTrue(any(b.get('table') == 'fk:product_editor_publications.product_id' for b in current['blockers']))
        self.assertEqual(await self.protected(), before)

    async def test_equivalent_gtin_conflict_owner_and_hidden_mapping_writer_enter_full_closure(self):
        # Neither receipt nor current row pins the second owner; discover its
        # UPC from the source EAN and include its hidden native mapping.
        async with self.base.transaction() as db:
            await db.execute(text("DELETE FROM atomer_catalog_proofs WHERE code='b'"))
            await db.execute(text("DELETE FROM atomer_catalog_source_cards WHERE code='b'"))
            await db.execute(text("UPDATE shop_products SET is_listed=false WHERE product_id=:id"), {'id': self.base.retired})
            await db.execute(text("""INSERT INTO product_editor_publications(id,product_id,shop_id,state,document)
                VALUES(:id,:p,:s,'uncertain','{}')"""),
                {'id': str(uuid4()), 'p': self.base.retired, 's': self.base.shop_id})
        current = await self.current()
        self.assertIn(self.base.retired, current['retained_card_ids'])
        self.assertEqual(current['mapping_count'], 2)
        self.assertFalse(current['eligible'])
        self.assertTrue(any(b.get('table') == 'fk:product_editor_publications.product_id' for b in current['blockers']))

    async def test_worker_and_phantom_writer_fences_abort_without_an_audit(self):
        payload = await self.payload()
        for key in (engine.WORKER_LOCK, campaigns.REVIEW_LOCK, IDENTITY_WRITE_LOCK, local.LOCAL_LOCK):
            async with self.sessions() as other:
                await other.execute(text('SELECT pg_advisory_xact_lock(:k)'), {'k': key})
                with self.subTest(key=key), self.assertRaises(engine.ReviewError):
                    await self.change(payload)
        async with self.sessions() as other:
            await other.execute(text("""INSERT INTO product_editor_publications(id,product_id,shop_id,state,document)
                VALUES(:id,:p,:s,'sending','{}')"""),
                {'id': str(uuid4()), 'p': self.base.keeper, 's': self.base.shop_id})
            with self.assertRaisesRegex(engine.ReviewError, 'writers_busy'):
                await self.change(payload)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM atomer_catalog_job_controls')), 0)
            self.assertFalse(await db.scalar(text('SELECT operator_withdrawn FROM atomer_catalog_jobs')))

    async def test_scanner_timeout_rolls_back_and_never_applies_unfenced_snapshot(self):
        payload = await self.payload()
        async def timeout(db, **kwargs):
            await db.rollback()
            raise scan.InventoryTimeout({'blockers': [{'code': 'synthetic_complete_inventory_timeout'}]})
        with patch.object(scan, 'inspect', side_effect=timeout):
            with self.assertRaisesRegex(engine.ReviewError, 'inventory_incomplete'):
                await self.change(payload)
            current = await self.current()
            self.assertFalse(current['eligible'])
            self.assertIsNone(current['snapshot_hash'])
        async with self.sessions() as db:
            self.assertTrue(await db.scalar(text('SELECT pg_try_advisory_xact_lock(:k)'), {'k': IDENTITY_WRITE_LOCK}))
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM atomer_catalog_job_controls')), 0)
        self.assertEqual((await self.change(payload))['status'], 'completed')

    async def test_concurrent_same_uuid_recovers_one_committed_handoff(self):
        payload = await self.payload()
        entered, release = asyncio.Event(), asyncio.Event()
        original = service.scope_inventory.writers
        async def held(*args, **kwargs):
            value = await original(*args, **kwargs)
            entered.set()
            await release.wait()
            return value
        with patch.object(service.scope_inventory, 'writers', side_effect=held):
            first = asyncio.create_task(self.change(payload))
            await asyncio.wait_for(entered.wait(), 10)
            try:
                with self.assertRaisesRegex(engine.ReviewError, 'catalog_worker_busy'):
                    await self.change(payload)
            finally:
                release.set()
            result = await first
        self.assertEqual(result, await self.change(payload))
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM atomer_catalog_job_controls')), 1)

    async def test_terminal_sql_survives_old_migration_replay_and_publication_stays_outstanding(self):
        await self.change()
        async with self.sessions() as db:
            old = await controls.get(db, self.campaign, 'v:P')
        self.assertFalse(old['replan_eligible'])
        self.assertIn('catalog_handoff_terminal', old['blockers'])
        replan = controls.JobControlRequest(request_id=uuid4(), family_key='v:P',
            expected_snapshot_hash=old['snapshot_hash'], selected_codes=old['selected_codes'],
            reason='Attempt to replan handed-off synthetic work.', confirmed=True)
        async with self.sessions() as db:
            with self.assertRaisesRegex(engine.ReviewError, 'catalog_handoff_terminal'):
                await controls.change(db, self.campaign, 'replan', replan, 'operator')
        for mutation in ("UPDATE atomer_catalog_jobs SET stage='pending',operator_withdrawn=false",
                         "UPDATE atomer_catalog_proofs SET state='verified'", "DELETE FROM atomer_catalog_source_cards",
                         "UPDATE atomer_catalog_job_controls SET actor='other'"):
            async with self.sessions() as db:
                with self.subTest(mutation=mutation), self.assertRaises(IntegrityError):
                    await db.execute(text(mutation))
                await db.rollback()
        # Reinstall the old generic function exactly as a repeat deployment
        # would. The independent terminal trigger must remain in force.
        import asyncpg
        connection = await asyncpg.connect(fixture.TEST_URL)
        try:
            await connection.execute(f'SET search_path TO "{self.base.schema}"')
            await connection.execute((self.base.sql_root / '049_atomer_catalog_job_control.sql').read_text())
            with self.assertRaisesRegex(asyncpg.CheckViolationError, 'catalog_handoff_terminal'):
                await connection.execute("UPDATE atomer_catalog_jobs SET problem='revive'")
            original = json.loads(await connection.fetchval('SELECT to_jsonb(t)::text FROM atomer_catalog_jobs t'))
            next_id = str(uuid4())
            changed = {**original, 'stage': 'pending', 'source_card_stage': 'pending', 'operator_withdrawn': False,
                'operator_control_revision': original['operator_control_revision'] + 1, 'operator_control_id': next_id}
            with self.assertRaisesRegex(asyncpg.CheckViolationError, 'catalog_handoff_terminal'):
                async with connection.transaction():
                    await connection.execute('''INSERT INTO atomer_catalog_job_controls
                        (request_id,campaign,family_key,action,request_hash,actor,request,before_job,after_job,evidence,result)
                        VALUES($1,$2,'v:P','replan',repeat('3',64),'synthetic-old-client','{}',$3::jsonb,$4::jsonb,'{}','{}')''',
                        next_id, self.campaign, json.dumps(original), json.dumps(changed))
                    await connection.execute("SELECT set_config('app.atomer_job_control_request',$1,true)", next_id)
                    await connection.execute("""UPDATE atomer_catalog_jobs SET stage='pending',source_card_stage='pending',
                        operator_withdrawn=false,operator_control_revision=operator_control_revision+1,operator_control_id=$1""", next_id)
            await connection.execute((self.base.sql_root / '050_atomer_catalog_handoff.sql').read_text())
            await connection.execute((self.base.sql_root / '050_atomer_catalog_handoff.sql').read_text())
        finally:
            await connection.close()
        await self.campaign_action('resume')
        async with self.sessions() as db:
            campaign = dict(await engine._one(db, 'SELECT * FROM atomer_catalog_campaigns'))
            with patch.object(engine, '_source', AsyncMock(side_effect=AssertionError('handoff cannot dispatch'))):
                self.assertFalse(await engine._step(db, campaign))
            await db.commit()
            self.assertEqual(await db.scalar(text('SELECT state FROM atomer_catalog_campaigns')), 'review')
            status = await engine.status(db)
            self.assertEqual(status['counts']['handoff_families'], 1)
            self.assertEqual(status['counts']['handoff_leaves'], 2)
            self.assertEqual(status['counts']['verified'], 0)
            self.assertEqual(status['counts']['partial'], 2)
