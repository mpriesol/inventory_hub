"""Campaign controls, durable replay and real PostgreSQL worker boundaries."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import asyncpg
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import text

from inventory_hub import database
from inventory_hub.database import get_session
from inventory_hub.routers import atomer_progress as routes
from inventory_hub.services import atomer_catalog_control as control
from inventory_hub.services import atomer_catalog_engine as engine
from inventory_hub.services import atomer_catalog_source_cards as local
from inventory_hub.services import product_consolidation_dependencies as dependencies
from inventory_hub.settings import settings
import test_atomer_catalog_engine as fixture


def request(**changes):
    return {'request_id': str(uuid4()), 'expected_revision': 0,
            'expected_updated_at': '2026-01-01T00:00:00+00:00',
            'reason': 'Reviewed maintenance of this exact campaign.', 'confirmed': True, **changes}


class ControlContractTests(unittest.TestCase):
    def test_confirmation_cas_and_reason_are_explicit_and_bounded(self):
        control.ControlRequest(**request())
        for changes in ({'confirmed': 1}, {'confirmed': 'true'}, {'confirmed': False},
                        {'expected_revision': True}, {'expected_revision': -1},
                        {'expected_updated_at': '2026-01-01T00:00:00'}, {'reason': 'short'},
                        {'reason': 'Review\ncontains control'}, {'reason': 'x' * 1001}, {'cancel_jobs': True}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                control.ControlRequest(**request(**changes))
        same = control.ControlRequest(**request(expected_updated_at='2026-01-01T01:00:00+01:00'))
        self.assertEqual(same.expected_updated_at, datetime(2026, 1, 1, tzinfo=timezone.utc))


class ControlRouteTests(unittest.TestCase):
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
        token = 'synthetic-campaign-control-' + 'x' * 24
        patcher = patch.object(settings, 'AI_CONTENT_ACCESS_TOKEN', SecretStr(token))
        patcher.start(); self.addCleanup(patcher.stop)
        self.headers = {'Authorization': 'Bearer ' + token}
        self.base = '/migration/atomer-progress/catalog-engine'
        self.campaign = 'a' * 64

    def test_every_control_route_requires_operator_before_database_and_has_no_store(self):
        calls = [('GET', f'{self.base}/campaigns/{self.campaign}/control', None, 'get'),
                 ('GET', f'{self.base}/control-requests/{uuid4()}', None, 'receipt'),
                 ('POST', f'{self.base}/campaigns/{self.campaign}/pause', request(), 'change'),
                 ('POST', f'{self.base}/campaigns/{self.campaign}/resume', request(), 'change')]
        for method, path, body, name in calls:
            with patch.object(control, name, AsyncMock(return_value={'status': 'completed'})) as operation:
                before = len(self.db_calls)
                denied = self.client.request(method, path, json=body)
                self.assertEqual(denied.status_code, 401)
                self.assertEqual(denied.headers['cache-control'], 'no-store')
                self.assertEqual(len(self.db_calls), before)
                operation.assert_not_awaited()
                allowed = self.client.request(method, path, json=body, headers=self.headers)
                self.assertEqual(allowed.status_code, 200, allowed.text)
                self.assertEqual(allowed.headers['cache-control'], 'no-store')
                if name == 'change':
                    self.assertEqual(operation.await_args.args[-1], 'operator-token')

    def test_cookie_mutations_require_same_origin_and_invalid_input_is_not_echoed(self):
        @self.app.middleware('http')
        async def principal(request, call_next):
            request.state.hub_user = {'id': 7, 'role': 'operator'}
            request.state.hub_bearer = False
            return await call_next(request)
        path = f'{self.base}/campaigns/{self.campaign}/pause'
        with patch.object(control, 'change', AsyncMock(return_value={'status': 'completed'})) as operation:
            denied = self.client.post(path, json=request())
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(self.db_calls, [])
            origin = {'Origin': 'http://testserver', 'X-Hub-Request': '1'}
            allowed = self.client.post(path, json=request(), headers=origin)
            self.assertEqual(allowed.status_code, 200, allowed.text)
            self.assertEqual(operation.await_args.args[-1], 'user:7')
            operation.reset_mock()
            for invalid_path, body in ((path, request(private='must-not-echo')),
                                       (path.replace('/pause', '/cancel'), request()),
                                       (path.replace(self.campaign, 'invalid'), request())):
                response = self.client.post(invalid_path, json=body, headers=origin)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.headers['cache-control'], 'no-store')
                self.assertNotIn('must-not-echo', response.text)
                operation.assert_not_awaited()


@unittest.skipUnless(fixture.TEST_URL, 'Requires dedicated localhost *_catalog_test database')
class CampaignControlDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixture.CampaignIntegrationTest.asyncSetUp(self)
        async with self.factory() as db:
            await engine.process(db, fixture.selection(), 'apply', 'operator')
        self.campaign = engine._campaign_id(fixture.selection())

    asyncTearDown = fixture.CampaignIntegrationTest.asyncTearDown

    async def current(self):
        async with self.factory() as db:
            return await control.get(db, self.campaign)

    async def payload(self, **changes):
        saved = await self.current()
        return control.ControlRequest(**request(**{'expected_revision': saved['revision'],
                    'expected_updated_at': saved['updated_at'], **changes}))

    async def change(self, action='pause', payload=None):
        payload = payload or await self.payload()
        async with self.factory() as db:
            return await control.change(db, self.campaign, action, payload, 'user:7')

    async def snapshot(self):
        # Compare complete stored rows, not just the fields updated by this feature.
        tables = ('atomer_catalog_jobs', 'atomer_catalog_proofs', 'atomer_catalog_source_cards',
                  'atomer_review_work', 'atomer_review_events')
        return {table: await self.conn.fetchval(f"SELECT coalesce(jsonb_agg(j ORDER BY j::text),'[]')::text "
                                               f'FROM (SELECT to_jsonb(t) j FROM {table} t) rows') for table in tables}

    @asynccontextmanager
    async def worker_session(self):
        async with self.factory() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def test_pause_and_resume_preserve_every_job_intent_and_quota_deadline(self):
        stages = ('pending', 'prepared', 'sending', 'observe', 'cards', 'related', 'fidelity',
                  'updating', 'partial', 'blocked', 'verified', 'excluded')
        for stage in stages:
            await self.conn.execute('''INSERT INTO atomer_catalog_jobs
                (campaign,family_key,priority,leaf_count,stage,guards,document,receipt,problem,attempts)
                VALUES ($1,$2,8,1,$3,'{"frozen":true}','{"product_id":101}',
                  '{"dispatched":true,"content_intent":{"product_id":101},"related_intent":{"product_id":101}}',
                  'Retain uncertain and prepared evidence',3)''', self.campaign, 'v:' + stage, stage)
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        await self.conn.execute("""UPDATE atomer_catalog_campaigns SET state='waiting',next_at=$2,
            failures=4,problem='Limit Upgates. HTTP 429' WHERE id=$1""", self.campaign, future)
        jobs = await self.snapshot()
        paused = await self.change()
        self.assertEqual((paused['before']['state'], paused['after']['state'], paused['after']['revision']),
                         ('waiting', 'paused', 1))
        self.assertTrue(paused['after']['operator_paused'])
        self.assertEqual(paused['after']['next_at'], future.isoformat())
        self.assertEqual(jobs, await self.snapshot())
        # Pausing the scheduler does not settle any matching frozen job.
        async with self.factory() as db:
            condition = dependencies._open_condition('atomer_catalog_jobs', {'stage'})
            open_stages = set(await db.scalars(text('SELECT stage FROM atomer_catalog_jobs t WHERE ' + condition)))
            self.assertTrue({'prepared', 'sending', 'observe', 'updating', 'blocked'} <= open_stages)
            audit_condition = dependencies._open_condition('atomer_catalog_controls', {'status'})
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM atomer_catalog_controls t WHERE ' + audit_condition)), 0)
        resumed = await self.change('resume')
        self.assertEqual((resumed['after']['state'], resumed['after']['revision'], resumed['after']['failures']),
                         ('waiting', 2, 4))
        self.assertFalse(resumed['after']['operator_paused'])
        self.assertEqual(resumed['after']['next_at'], future.isoformat())
        self.assertEqual(await self.conn.fetchval('SELECT problem FROM atomer_catalog_campaigns'), 'Limit Upgates. HTTP 429')
        self.assertEqual(jobs, await self.snapshot())

    async def test_original_uuid_replays_after_resume_and_conflicting_reuse_cannot_mutate(self):
        payload = await self.payload()
        saved = await self.change(payload=payload)
        await self.change('resume')
        current = await self.current()
        self.assertEqual(await self.change(payload=payload), saved)
        self.assertEqual(await self.current(), current)
        async with self.factory() as db:
            self.assertEqual(await control.receipt(db, payload.request_id), saved)
        for action, changed in (('resume', payload),
                                ('pause', payload.model_copy(update={'reason': 'A distinct reviewed maintenance reason.'}))):
            with self.assertRaisesRegex(engine.ReviewError, 'catalog_control_request_conflict'):
                await self.change(action, changed)
        self.assertEqual(await self.current(), current)
        self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM atomer_catalog_controls'), 2)

    async def test_stale_cas_terminal_states_and_other_active_campaign_fail_without_receipt(self):
        stale = await self.payload()
        await self.conn.execute('UPDATE atomer_catalog_campaigns SET updated_at=updated_at+interval \'1 second\'')
        with self.assertRaisesRegex(engine.ReviewError, 'catalog_control_changed'):
            await self.change(payload=stale)
        await self.conn.execute("UPDATE atomer_catalog_campaigns SET state='done'")
        with self.assertRaisesRegex(engine.ReviewError, 'catalog_campaign_not_runnable'):
            await self.change()
        self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM atomer_catalog_controls'), 0)
        await self.conn.execute("UPDATE atomer_catalog_campaigns SET state='running'")
        await self.change()
        await self.conn.execute('''INSERT INTO atomer_catalog_campaigns
            (id,run_id,source_sha,admin_sha,shop_fingerprint,actor,selection,state,checked_run_id)
            SELECT repeat('f',64),run_id,source_sha,admin_sha,shop_fingerprint,actor,selection,'running',checked_run_id
            FROM atomer_catalog_campaigns WHERE id=$1''', self.campaign)
        with self.assertRaisesRegex(engine.ReviewError, 'catalog_other_campaign_active'):
            await self.change('resume')
        self.assertTrue((await self.current())['operator_paused'])
        self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM atomer_catalog_controls'), 1)

    async def test_resume_of_prior_automatic_pause_restores_paused_without_waking(self):
        await self.conn.execute("UPDATE atomer_catalog_campaigns SET state='paused',failures=8,problem='Keep failure'")
        await self.change()
        resumed = await self.change('resume')
        self.assertEqual((resumed['after']['state'], resumed['after']['operator_paused'], resumed['after']['failures']),
                         ('paused', False, 8))

    async def test_worker_session_lock_survives_intent_commit_and_refuses_pause(self):
        payload = await self.payload()
        async with self.pg.connect() as worker:
            await worker.execute(text('SELECT pg_advisory_lock(:key)'), {'key': engine.WORKER_LOCK})
            await worker.commit()  # Worker retains its session lock across a send boundary.
            try:
                async with self.factory() as intent:
                    await intent.execute(text("UPDATE atomer_catalog_jobs SET stage='observe',receipt=CAST(:receipt AS jsonb)"),
                                         {'receipt': json.dumps({'dispatched': True})})
                    await intent.commit()
                jobs = await self.snapshot()
                with self.assertRaisesRegex(engine.ReviewError, 'catalog_worker_busy'):
                    await self.change(payload=payload)
                self.assertFalse((await self.current())['operator_paused'])
                self.assertEqual(jobs, await self.snapshot())
                self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM atomer_catalog_controls'), 0)
            finally:
                await worker.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': engine.WORKER_LOCK})
                await worker.commit()
        await self.change(payload=payload)
        self.assertEqual(jobs, await self.snapshot())

    async def test_review_and_identity_lock_contention_returns_busy_without_leaking_worker_lock(self):
        for key, code in ((control.REVIEW_LOCK, 'catalog_review_busy'),
                          (control.IDENTITY_WRITE_LOCK, 'catalog_identity_busy')):
            async with self.pg.connect() as blocker:
                await blocker.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})
                with self.assertRaisesRegex(engine.ReviewError, code):
                    await self.change()
                self.assertTrue(await self.conn.fetchval('SELECT pg_try_advisory_lock($1)', engine.WORKER_LOCK))
                await self.conn.execute('SELECT pg_advisory_unlock($1)', engine.WORKER_LOCK)
                await blocker.rollback()
        self.assertFalse((await self.current())['operator_paused'])
        self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM atomer_catalog_controls'), 0)

    async def test_pause_locks_cover_commit_and_waiting_apply_cannot_wake_after_ack(self):
        ready, release, registered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        payload = await self.payload()
        jobs = await self.snapshot()
        original_review = engine.review._lock_current_run
        async def register_lock(db, run):
            registered.set()
            return await original_review(db, run)
        async def register():
            async with self.factory() as db:
                return await engine.process(db, fixture.selection(), 'apply', 'operator')
        async with self.factory() as pausing:
            original_commit = pausing.commit
            async def held_commit():
                ready.set()
                await release.wait()
                await original_commit()
            with patch.object(pausing, 'commit', held_commit), \
                    patch.object(database, '_engine', self.pg), \
                    patch.object(database, 'get_session_context', self.worker_session), \
                    patch.object(engine, '_step', AsyncMock()) as remote_step, \
                    patch.object(local, 'prepare', AsyncMock()) as local_step:
                task = asyncio.create_task(control.change(pausing, self.campaign, 'pause', payload, 'user:7'))
                try:
                    await asyncio.wait_for(ready.wait(), 3)
                    self.assertFalse(await engine.cycle())
                    self.assertFalse(await local.cycle())
                    with patch.object(engine.review, '_lock_current_run', register_lock):
                        normal = asyncio.create_task(register())
                        await asyncio.wait_for(registered.wait(), 3)
                        self.assertFalse(normal.done())
                        release.set()
                        await asyncio.wait_for(task, 3)
                        with self.assertRaisesRegex(engine.ReviewError, 'catalog_operator_paused'):
                            await asyncio.wait_for(normal, 3)
                    self.assertFalse(await engine.cycle())
                    self.assertFalse(await local.cycle())
                    remote_step.assert_not_awaited()
                    local_step.assert_not_awaited()
                finally:
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(jobs, await self.snapshot())

    async def test_local_worker_rollback_then_pause_does_not_write_retry_metadata_after_ack(self):
        rolled_back, release = asyncio.Event(), asyncio.Event()
        @asynccontextmanager
        async def failing_session():
            async with self.factory() as db:
                original = db.rollback
                async def intercepted():
                    await original()
                    rolled_back.set()
                    await release.wait()
                with patch.object(db, 'rollback', intercepted):
                    yield db
                    await db.commit()
        jobs = await self.snapshot()
        with patch.object(database, '_engine', self.pg), \
                patch.object(database, 'get_session_context', failing_session), \
                patch.object(local, 'prepare', AsyncMock(side_effect=RuntimeError('synthetic local failure'))):
            task = asyncio.create_task(local.cycle())
            try:
                await asyncio.wait_for(rolled_back.wait(), 3)
                await self.change()
                release.set()
                self.assertFalse(await asyncio.wait_for(task, 3))
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(jobs, await self.snapshot())

    async def test_schema_replay_preserves_pause_and_audit_is_immutable(self):
        saved = await self.change()
        current, jobs = await self.current(), await self.snapshot()
        root = Path(__file__).resolve().parents[2] / 'infra' / 'db-init'
        await self.conn.execute((root / '048_atomer_catalog_control.sql').read_text())
        self.assertEqual(await self.current(), current)
        self.assertEqual(await self.snapshot(), jobs)
        for statement in ("UPDATE atomer_catalog_controls SET actor='changed'", 'DELETE FROM atomer_catalog_controls',
                          "UPDATE atomer_catalog_campaigns SET state='running'"):
            with self.assertRaises(asyncpg.CheckViolationError):
                await self.conn.execute(statement)
        async with self.factory() as db:
            self.assertEqual(await control.receipt(db, saved['request_id']), saved)
        self.assertEqual(await self.current(), current)
