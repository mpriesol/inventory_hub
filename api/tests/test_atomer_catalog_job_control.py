"""One-job lifecycle controls with real worker, row and phantom boundaries."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from inventory_hub.database import get_session
from inventory_hub.routers import atomer_progress as routes
from inventory_hub.services import atomer_catalog_control as campaign_control
from inventory_hub.services import atomer_catalog_job_control as control
from inventory_hub.services import atomer_catalog_engine as engine
from inventory_hub.services import atomer_catalog_source_cards as local
from inventory_hub.services import atomer_catalog_recheck as recheck
from inventory_hub.services import product_consolidation_dependencies as dependencies
from inventory_hub.settings import settings
import test_atomer_catalog_engine as fixture


def request(**changes):
    return dict(request_id=str(uuid4()), family_key='v:P', expected_snapshot_hash='a' * 64,
                selected_codes=['a', 'b'], reason='This exact unsent plan is handed to manual review.', confirmed=True, **changes)


class JobControlContractTests(unittest.TestCase):
    def test_explicit_uuid_scope_hash_and_confirmation(self):
        control.JobControlRequest(**request())
        for changes in ({'confirmed': 1}, {'confirmed': 'true'}, {'confirmed': False},
                        {'selected_codes': ['a', 'a']}, {'selected_codes': []}, {'selected_codes': [1]},
                        {'expected_snapshot_hash': 'unknown'}, {'reason': 'bad\nreason'},
                        {'override': True}, {'family_key': ''}):
            data = {**request(), **changes}
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                control.JobControlRequest(**data)

    def test_routes_require_operator_before_database_and_are_no_store(self):
        db_calls = []
        async def session():
            db_calls.append(True)
            yield SimpleNamespace()
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[get_session] = session
        base = '/migration/atomer-progress/catalog-engine'
        cid = 'a' * 64
        calls = [('GET', f'{base}/campaigns/{cid}/jobs/control?family=v:P', None, 'get'),
                 ('GET', f'{base}/job-control-requests/{uuid4()}', None, 'receipt'),
                 ('POST', f'{base}/campaigns/{cid}/jobs/withdraw', request(), 'change'),
                 ('POST', f'{base}/campaigns/{cid}/jobs/replan', request(), 'change')]
        token = 'synthetic-job-control-' + 'x' * 24
        with patch.object(settings, 'AI_CONTENT_ACCESS_TOKEN', SecretStr(token)), TestClient(app) as client:
            for method, path, body, name in calls:
                with patch.object(control, name, AsyncMock(return_value={'status': 'completed'})) as operation:
                    count = len(db_calls)
                    denied = client.request(method, path, json=body)
                    self.assertEqual(denied.status_code, 401)
                    self.assertEqual(denied.headers['cache-control'], 'no-store')
                    self.assertEqual(len(db_calls), count)
                    operation.assert_not_awaited()
                    response = client.request(method, path, json=body, headers={'Authorization': 'Bearer ' + token})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.headers['cache-control'], 'no-store')
                    if name == 'change':
                        self.assertEqual(operation.await_args.args[-1], 'operator-token')


@unittest.skipUnless(fixture.TEST_URL, 'Requires dedicated localhost *_catalog_test database')
class JobControlDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixture.CampaignIntegrationTest.asyncSetUp(self)
        await self.conn.execute('ALTER TABLE products ADD COLUMN sku text, ADD COLUMN created_from_source text')
        await self.conn.execute('''CREATE TABLE product_editor_audit(id bigint PRIMARY KEY,product_id bigint,
                                   before_data jsonb NOT NULL,after_data jsonb NOT NULL)''')
        self.campaign = engine._campaign_id(fixture.selection())
        async with self.factory() as db:
            await engine.process(db, fixture.selection(), 'apply', 'operator')
            c = dict(await engine._one(db, 'SELECT * FROM atomer_catalog_campaigns'))
            j = dict(await engine._one(db, 'SELECT * FROM atomer_catalog_jobs'))
            rows, group = await engine._family(db, 1, 'v:P')
            guards = await engine._claim(db, c, j, rows, group)
            await engine._job(db, c['id'], 'v:P', 'pending', guards=guards)
            await db.commit()
            current = await campaign_control.get(db, c['id'])
            payload = campaign_control.ControlRequest(request_id=uuid4(), expected_revision=current['revision'],
                expected_updated_at=current['updated_at'], reason='Pause before reviewing one exact unsent job.', confirmed=True)
            await campaign_control.change(db, c['id'], 'pause', payload, 'operator')

    asyncTearDown = fixture.CampaignIntegrationTest.asyncTearDown

    async def current(self):
        async with self.factory() as db:
            return await control.get(db, self.campaign, 'v:P')

    async def payload(self):
        current = await self.current()
        return control.JobControlRequest(**{**request(), 'expected_snapshot_hash': current['snapshot_hash'],
                                            'selected_codes': current['selected_codes']})

    async def change(self, action='withdraw', payload=None):
        payload = payload or await self.payload()
        async with self.factory() as db:
            return await control.change(db, self.campaign, action, payload, 'user:7')

    async def protected(self):
        tables = ('atomer_catalog_proofs', 'atomer_catalog_source_cards', 'atomer_review_work', 'atomer_review_events')
        return {table: await self.conn.fetchval(f"SELECT coalesce(jsonb_agg(t ORDER BY to_jsonb(t)::text),'[]')::text FROM {table} t")
                for table in tables}

    async def test_withdraw_and_explicit_replan_preserve_all_proofs_and_review_history(self):
        await self.conn.execute("""UPDATE atomer_catalog_jobs SET attempts=6,receipt='{"isolate_phase":"pending"}',
            source_card_stage='blocked',source_card_attempts=3,problem='registry temporarily unavailable'""")
        await self.conn.execute("""INSERT INTO atomer_catalog_source_cards
            (campaign,family_key,code,run_id,state,evidence) VALUES($1,'v:P','a',1,'blocked',
            '{"status":"blocked","reason":"atomer_source_card_requires_review","card_id":null,"hub_sku":null,"xtrek":null,"biketrek":null}')""", self.campaign)
        await self.conn.execute("""INSERT INTO atomer_catalog_proofs(campaign,family_key,code,state,evidence)
            VALUES($1,'v:P','b','blocked','{"reason":"sibling manual review"}')""", self.campaign)
        protected = await self.protected()
        original = await self.conn.fetchval('SELECT to_jsonb(t)::text FROM atomer_catalog_jobs t')
        state = await self.current()
        self.assertTrue(state['eligible'], state)
        self.assertFalse(state['replan_eligible'])
        result = await self.change()
        self.assertTrue(result['operator_withdrawn'])
        self.assertEqual(result['stage'], 'excluded')
        self.assertEqual(protected, await self.protected())
        self.assertEqual(json.loads(original), json.loads(await self.conn.fetchval(
            'SELECT before_job::text FROM atomer_catalog_job_controls')))
        self.assertEqual(await self.conn.fetchval('SELECT to_jsonb(j)=a.after_job FROM atomer_catalog_jobs j '
            'JOIN atomer_catalog_job_controls a ON a.request_id=j.operator_control_id'), True)
        self.assertTrue((await self.current())['replan_eligible'])
        restored = await self.change('replan')
        self.assertFalse(restored['operator_withdrawn'])
        self.assertEqual(restored['job_revision'], 2)
        self.assertEqual(protected, await self.protected())
        self.assertEqual(await self.conn.fetchval('SELECT document FROM atomer_catalog_jobs'), None)
        self.assertEqual(await self.conn.fetchval('SELECT receipt::text FROM atomer_catalog_jobs'), '{}')
        self.assertTrue(await self.conn.fetchval('SELECT operator_paused FROM atomer_catalog_campaigns'))
        self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM atomer_catalog_job_controls'), 2)

    async def test_uuid_replay_after_replan_and_payload_conflict(self):
        payload = await self.payload()
        result = await self.change(payload=payload)
        await self.change('replan')
        state = await self.current()
        self.assertEqual(await self.change(payload=payload), result)
        self.assertEqual(await self.current(), state)
        changed = payload.model_copy(update={'reason': 'A different handoff reason cannot reuse the UUID.'})
        with self.assertRaisesRegex(engine.ReviewError, 'catalog_job_control_request_conflict'):
            await self.change(payload=changed)
        async with self.factory() as db:
            self.assertEqual(await control.receipt(db, payload.request_id), result)

    async def test_effects_unknown_shapes_and_nonpending_stages_never_withdraw(self):
        async with self.factory() as db:
            base = await control._snapshot(db, self.campaign, 'v:P')
        cases = [({'stage': 'prepared'}, 'catalog_job_not_unsent_pending'),
                 ({'stage': 'sending'}, 'catalog_job_not_unsent_pending'),
                 ({'document': {'payload': {'code': 'P'}}}, 'catalog_job_document_present'),
                 ({'receipt': {'dispatched': True}}, 'catalog_job_effects_unverified'),
                 ({'receipt': {'content_intent': {}}}, 'catalog_job_effects_unverified'),
                 ({'receipt': {'native_origin': None}}, 'catalog_job_effects_unverified'),
                 ({'receipt': {'unknown': False}}, 'catalog_job_effects_unverified'),
                 ({'receipt': []}, 'catalog_job_effects_unverified'),
                 ({'guards': None}, 'catalog_job_selection_unverified'),
                 ({'guards': {'rows': None}}, 'catalog_job_selection_unverified'),
                 ({'future_intent': {}}, 'catalog_job_schema_unverified'),
                 ({'source_card_stage': 'checked'}, 'catalog_job_source_lane_unverified')]
        for fields, expected in cases:
            data = deepcopy(base); data['job'].update(fields)
            with self.subTest(fields=fields):
                self.assertIn(expected, control.inspect(data)['blockers'])
        for evidence in ({'reason': 'blocked', 'creation_evidence': {}}, {'reason': 'blocked', 'card_id': 44},
                         {'reason': 'blocked', 'xtrek': {'product_id': 9}}, {'status': 'blocked'},
                         {'reason': 'source_card_readback_failed', 'card_id': None, 'xtrek': None, 'biketrek': None}):
            self.assertFalse(control._negative_evidence(evidence, source=True))
        unknown = deepcopy(base)
        unknown['proofs'] = [{'campaign': self.campaign, 'family_key': 'v:P', 'code': 'a', 'state': 'blocked',
            'evidence': {'reason': 'manual review'}, 'checked_at': '2026-01-01T00:00:00Z', 'new_effect': {}}]
        self.assertIn('catalog_job_effects_unverified', control.inspect(unknown)['blockers'])
        await self.conn.execute("UPDATE atomer_catalog_jobs SET receipt='{\"dispatched\":true}'")
        with self.assertRaisesRegex(engine.ReviewError, 'catalog_job_effects_unverified'):
            await self.change()
        self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM atomer_catalog_job_controls'), 0)

    async def test_stale_hash_selected_scope_and_changed_reviews_fail_closed(self):
        payload = await self.payload()
        await self.conn.execute('UPDATE atomer_catalog_jobs SET attempts=attempts+1')
        with self.assertRaisesRegex(engine.ReviewError, 'catalog_job_control_changed'):
            await self.change(payload=payload)
        payload = await self.payload()
        with self.assertRaisesRegex(engine.ReviewError, 'catalog_job_control_changed'):
            await self.change(payload=payload.model_copy(update={'selected_codes': ['a']}))
        await self.conn.execute("UPDATE atomer_review_work SET revision=revision+1 WHERE key='a'")
        self.assertIn('catalog_job_reviews_changed', (await self.current())['blockers'])
        with self.assertRaisesRegex(engine.ReviewError, 'catalog_job_reviews_changed'):
            await self.change()
        self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM atomer_catalog_job_controls'), 0)

    async def test_hidden_source_allocation_is_not_erased_by_later_blocked_receipt(self):
        await self.conn.execute("INSERT INTO products VALUES(81,'a','atomer')")
        self.assertIn('catalog_job_prior_source_allocation', (await self.current())['blockers'])
        with self.assertRaisesRegex(engine.ReviewError, 'catalog_job_prior_source_allocation'):
            await self.change()
        self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM atomer_catalog_job_controls'), 0)

    async def test_supported_sku_rename_cannot_hide_historical_source_allocation(self):
        await self.conn.execute("INSERT INTO products VALUES(81,'renamed-unmapped-card','atomer')")
        await self.conn.execute('''INSERT INTO product_editor_audit VALUES(7,81,
            '{"sku":"a"}','{"sku":"renamed-unmapped-card"}')''')
        state = await self.current()
        self.assertIn('catalog_job_prior_source_allocation', state['blockers'])
        with self.assertRaisesRegex(engine.ReviewError, 'catalog_job_prior_source_allocation'):
            await self.change()
        self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM atomer_catalog_job_controls'), 0)

    async def test_reverse_proof_row_lock_returns_busy_without_deadlock_or_partial_audit(self):
        await self.conn.execute("""INSERT INTO atomer_catalog_proofs(campaign,family_key,code,state,evidence)
            VALUES($1,'v:P','a','blocked','{"reason":"sibling is reserved"}')""", self.campaign)
        payload = await self.payload()
        async with self.factory() as writer:
            await writer.execute(text("SELECT * FROM atomer_catalog_proofs WHERE code='a' FOR UPDATE"))
            # A normal UPDATE owns the proof row before its BEFORE trigger
            # requests job SHARE. Control must not wait in the inverse order.
            with self.assertRaisesRegex(engine.ReviewError, 'catalog_job_rows_busy'):
                await asyncio.wait_for(self.change(payload=payload), 3)
            await writer.execute(text("UPDATE atomer_catalog_proofs SET checked_at=clock_timestamp() WHERE code='a'"))
            await writer.commit()
        self.assertFalse((await self.current())['operator_withdrawn'])
        self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM atomer_catalog_job_controls'), 0)

    async def test_all_advisory_boundaries_refuse_inflight_work_and_release_partial_locks(self):
        for key, code in ((engine.WORKER_LOCK, 'catalog_worker_busy'),
                          (campaign_control.REVIEW_LOCK, 'catalog_review_busy'),
                          (campaign_control.IDENTITY_WRITE_LOCK, 'catalog_identity_busy'),
                          (control.REFRESH_LOCK, 'refresh_in_progress'),
                          (local.LOCAL_LOCK, 'catalog_source_worker_busy')):
            async with self.pg.connect() as worker:
                await worker.execute(text('SELECT pg_advisory_lock(:key)'), {'key': key})
                await worker.commit()  # Session survives the ordinary send/intent commit.
                try:
                    with self.assertRaisesRegex(engine.ReviewError, code):
                        await self.change()
                    self.assertFalse((await self.current())['operator_withdrawn'])
                finally:
                    await worker.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': key})
                    await worker.commit()
        self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM atomer_catalog_job_controls'), 0)

    async def test_mid_transaction_failure_rolls_back_audit_and_state_together(self):
        before = await self.current()
        original = engine._exec
        async def fail_after_audit(db, sql, **args):
            if 'UPDATE atomer_catalog_jobs SET' in sql:
                raise RuntimeError('synthetic control write failure')
            return await original(db, sql, **args)
        with patch.object(engine, '_exec', side_effect=fail_after_audit):
            with self.assertRaisesRegex(RuntimeError, 'synthetic control write failure'):
                await self.change()
        self.assertEqual(await self.current(), before)
        self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM atomer_catalog_job_controls'), 0)

    async def test_sticky_sql_fence_rejects_direct_requeue_proof_mutation_and_audit_changes(self):
        await self.change()
        statements = ["UPDATE atomer_catalog_jobs SET stage='pending'",
                      "UPDATE atomer_catalog_jobs SET operator_withdrawn=false",
                      "UPDATE atomer_catalog_jobs SET receipt=receipt||'{\"dispatched\":true}'",
                      'DELETE FROM atomer_catalog_jobs',
                      "UPDATE atomer_catalog_job_controls SET actor='different'",
                      'DELETE FROM atomer_catalog_job_controls']
        for sql in statements:
            with self.subTest(sql=sql), self.assertRaises(Exception):
                await self.conn.execute(sql)
        with self.assertRaisesRegex(Exception, 'catalog_job_withdrawn_proof_preserved'):
            await self.conn.execute("""INSERT INTO atomer_catalog_proofs(campaign,family_key,code,state,evidence)
                VALUES($1,'v:P','a','verified','{"card_id":99}')""", self.campaign)
        async with self.factory() as db:
            condition = dependencies._open_condition('atomer_catalog_jobs', {'stage'})
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM atomer_catalog_jobs t WHERE ' + condition)), 0)
            settled = dependencies._open_condition('atomer_catalog_job_controls', {'status'})
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM atomer_catalog_job_controls t WHERE ' + settled)), 0)

    async def test_campaign_resume_bulk_registration_rollover_local_retry_and_stale_candidate_cannot_revive(self):
        await self.change()
        before = await self.conn.fetchval("SELECT to_jsonb(t)::text FROM atomer_catalog_jobs t WHERE family_key='v:P'")
        async with self.factory() as db:
            current = await campaign_control.get(db, self.campaign)
            req = campaign_control.ControlRequest(request_id=uuid4(), expected_revision=current['revision'],
                expected_updated_at=current['updated_at'], reason='Resume the rest of the campaign only.', confirmed=True)
            await campaign_control.change(db, self.campaign, 'resume', req, 'operator')
            await engine.process(db, fixture.selection(), 'apply', 'operator')
            await engine._sync_families(db, self.campaign, 1)
            await local.retry_new_evidence(db, self.campaign, {'zero_barcode_sentinels': {'a': '12'}}, {'a'})
            c = dict(await engine._one(db, 'SELECT * FROM atomer_catalog_campaigns'))
            self.assertNotIn('v:P', {row['family_key'] for row in await local.due(db, c)})
            await db.commit()
        self.assertEqual(await self.conn.fetchval("SELECT to_jsonb(t)::text FROM atomer_catalog_jobs t WHERE family_key='v:P'"), before)
        async with self.factory() as db:
            c = dict(await engine._one(db, 'SELECT * FROM atomer_catalog_campaigns'))
            j = dict(await engine._one(db, "SELECT * FROM atomer_catalog_jobs WHERE family_key='v:P'"))
            for operation in (engine._guarded,):
                with self.assertRaisesRegex(engine.ReviewError, 'catalog_job_withdrawn'):
                    await operation(db, c, j)
            with self.assertRaisesRegex(engine.ReviewError, 'catalog_job_withdrawn'):
                await local.prepare(db, c, [j])
            with self.assertRaisesRegex(engine.ReviewError, 'catalog_job_withdrawn'):
                await recheck.reschedule(db, c, j, 'unknown', 'apply', 'operator')

    async def test_proof_phantom_waits_for_withdrawal_commit_then_fails(self):
        ready, release = asyncio.Event(), asyncio.Event()
        original = engine._exec
        async def hold(db, sql, **args):
            value = await original(db, sql, **args)
            if 'INSERT INTO atomer_catalog_job_controls' in sql:
                ready.set(); await release.wait()
            return value
        payload = await self.payload()
        async def phantom():
            async with self.factory() as db:
                await db.execute(text("""INSERT INTO atomer_catalog_proofs(campaign,family_key,code,state,evidence)
                    VALUES(:c,'v:P','a','verified',CAST(:e AS jsonb))"""),
                    {'c': self.campaign, 'e': json.dumps({'card_id': 77})})
                await db.commit()
        with patch.object(engine, '_exec', side_effect=hold):
            withdraw = asyncio.create_task(self.change(payload=payload))
            await asyncio.wait_for(ready.wait(), 5)
            writer = asyncio.create_task(phantom())
            try:
                await asyncio.sleep(.05)
                self.assertFalse(writer.done())
            finally:
                release.set()
            await withdraw
            with self.assertRaisesRegex(DBAPIError, 'catalog_job_withdrawn_proof_preserved'):
                await writer
        self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM atomer_catalog_proofs'), 0)

    async def test_worker_lock_is_held_through_durable_commit_and_audit_replay_is_immutable(self):
        ready, release = asyncio.Event(), asyncio.Event()
        original = engine._exec
        async def hold(db, sql, **args):
            result = await original(db, sql, **args)
            if 'UPDATE atomer_catalog_jobs SET' in sql:
                ready.set(); await release.wait()
            return result
        with patch.object(engine, '_exec', side_effect=hold):
            task = asyncio.create_task(self.change())
            await asyncio.wait_for(ready.wait(), 5)
            self.assertFalse(await self.conn.fetchval('SELECT pg_try_advisory_lock($1)', engine.WORKER_LOCK))
            self.assertFalse(await self.conn.fetchval('SELECT pg_try_advisory_lock($1)', control.REFRESH_LOCK))
            self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM atomer_catalog_job_controls'), 0)
            release.set(); await task
        self.assertTrue(await self.conn.fetchval('SELECT pg_try_advisory_lock($1)', engine.WORKER_LOCK))
        await self.conn.execute('SELECT pg_advisory_unlock($1)', engine.WORKER_LOCK)
        root = Path(__file__).resolve().parents[2] / 'infra/db-init'
        before = await self.conn.fetchval('SELECT to_jsonb(t)::text FROM atomer_catalog_job_controls t')
        for name in ('040_atomer_catalog_campaign.sql', '041_atomer_catalog_content.sql',
                     '043_atomer_source_cards.sql', '048_atomer_catalog_control.sql', '049_atomer_catalog_job_control.sql'):
            await self.conn.execute((root / name).read_text())
        self.assertEqual(await self.conn.fetchval('SELECT to_jsonb(t)::text FROM atomer_catalog_job_controls t'), before)

    async def test_get_is_database_read_only_and_never_reconciles_or_sends(self):
        async with self.factory() as db:
            with patch.object(engine, '_source', AsyncMock(side_effect=AssertionError('network forbidden'))) as source:
                value = await control.get(db, self.campaign, 'v:P')
                self.assertTrue(value['eligible'])
                source.assert_not_awaited()
                self.assertEqual(await db.scalar(text('SHOW transaction_read_only')), 'on')
                self.assertEqual(await db.scalar(text('SHOW transaction_isolation')), 'repeatable read')
                with self.assertRaises(DBAPIError):
                    await db.execute(text("UPDATE atomer_catalog_jobs SET problem='unexpected'"))
