"""Real PostgreSQL concurrency and notification lifecycle in an isolated schema."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from inventory_hub.attention_types import AttentionEvent, AttentionAction, AttentionRuleInput
from inventory_hub.services import attention

URL = os.environ.get('CATALOG_TEST_DATABASE_URL', '')


@unittest.skipUnless(URL, 'Requires an isolated localhost *_catalog_test PostgreSQL database')
class AttentionDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(URL)
        if parsed.hostname not in ('localhost', '127.0.0.1') or not parsed.path.endswith('_catalog_test'):
            raise RuntimeError('Dedicated localhost *_catalog_test database required')
        self.schema = 'attention_test_' + uuid4().hex
        connection = await asyncpg.connect(URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            sql = (Path(__file__).resolve().parents[2] / 'infra/db-init/053_attention.sql').read_text()
            await connection.execute(sql); await connection.execute(sql)
        finally:
            await connection.close()
        self.engine = create_async_engine(URL.replace('postgresql://', 'postgresql+asyncpg://', 1), poolclass=NullPool,
            connect_args={'server_settings': {'search_path': self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.logs = patch.object(attention.operation_log, 'emit'); self.emit = self.logs.start()
        self.addCleanup(self.logs.stop)

    async def asyncTearDown(self):
        await self.engine.dispose()
        connection = await asyncpg.connect(URL)
        try:
            await connection.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            await connection.close()

    @asynccontextmanager
    async def tx(self):
        async with self.sessions() as db:
            async with db.begin():
                yield db

    def payload(self, sequence, active=True, scope='husky', kind='supplier_availability.partial'):
        return AttentionEvent(kind=kind, scope=scope, sequence=sequence, active=active,
            details={'run_id': sequence, 'total': 100, 'accepted': 1, 'rejected': 99})

    async def publish(self, sequence, active=True, scope='husky'):
        async with self.tx() as db:
            return await attention.report(db, self.payload(sequence, active, scope))

    async def page(self, state='active', **kwargs):
        async with self.tx() as db:
            return await attention.overview(db, state, **kwargs)

    async def test_ignore_repeat_resolve_recur_restore_and_stale_action(self):
        item = await self.publish(1)
        async with self.tx() as db:
            await attention.action(db, item['id'], AttentionAction(expected_revision=item['revision'], action='ignore'))
        await self.publish(2)
        self.assertEqual((await self.page())['total'], 0)
        ignored = (await self.page('ignored'))['items'][0]
        self.assertEqual(ignored['occurrences'], 2)
        with self.assertRaises(attention.AttentionError):
            async with self.tx() as db:
                await attention.action(db, item['id'], AttentionAction(expected_revision=1, action='restore'))
        await self.publish(3, False)
        self.assertEqual((await self.page('resolved'))['total'], 1)
        new = await self.publish(4)
        visible = (await self.page())['items'][0]
        self.assertEqual(visible['id'], item['id']); self.assertEqual(visible['episode'], 2)
        self.assertEqual(visible['occurrences'], 1); self.assertIsNone(visible['ignored_at'])
        async with self.tx() as db:
            await attention.action(db, item['id'], AttentionAction(expected_revision=new['revision'], action='ignore'))
            await attention.action(db, item['id'], AttentionAction(expected_revision=new['revision'] + 1, action='restore'))
        self.assertEqual((await self.page())['total'], 1)

    async def test_idempotent_concurrent_delivery_and_old_results_cannot_reopen(self):
        results = await asyncio.gather(self.publish(1), self.publish(1))
        self.assertEqual(sum(not r['replayed'] for r in results), 1)
        self.assertEqual((await self.page())['items'][0]['occurrences'], 1)
        await self.publish(3, False)
        await self.publish(2)
        self.assertEqual((await self.page())['total'], 0)
        with self.assertRaises(attention.AttentionError) as error:
            await self.publish(3, True)
        self.assertEqual(error.exception.code, 'attention_sequence_conflict')

    async def test_global_and_resource_rules_suppress_without_stopping_logging(self):
        kind = 'supplier_availability.partial'
        async with self.tx() as db:
            await attention.set_rule(db, kind, '*', AttentionRuleInput(expected_revision=0, enabled=True, min_occurrences=2))
        await self.publish(1)
        self.assertEqual((await self.page())['total'], 0)
        self.assertEqual((await self.page('muted'))['total'], 1)
        await self.publish(2)
        self.assertEqual((await self.page())['total'], 1)
        async with self.tx() as db:
            await attention.set_rule(db, kind, 'husky', AttentionRuleInput(expected_revision=0, enabled=False, min_occurrences=1))
        count = self.emit.call_count
        await self.publish(3)
        self.assertGreater(self.emit.call_count, count)
        self.assertEqual((await self.page())['total'], 0)
        async with self.tx() as db:
            await attention.set_rule(db, kind, 'husky', AttentionRuleInput(expected_revision=1, enabled=True, min_occurrences=1))
            await attention.set_rule(db, kind, '*', AttentionRuleInput(expected_revision=1, enabled=False, min_occurrences=1))
        self.assertEqual((await self.page())['total'], 0, 'Global disable also suppresses resource overrides')
        async with self.tx() as db:
            await attention.set_rule(db, kind, '*', AttentionRuleInput(expected_revision=2, enabled=True, min_occurrences=1))
        self.assertEqual((await self.page())['total'], 1)
        with self.assertRaises(attention.AttentionError):
            async with self.tx() as db:
                await attention.set_rule(db, kind, '*', AttentionRuleInput(expected_revision=1, enabled=False, min_occurrences=1))

    async def test_transaction_rollback_emits_no_false_notification(self):
        with self.assertRaises(RuntimeError):
            async with self.tx() as db:
                await attention.report(db, self.payload(1))
                self.assertEqual(self.emit.call_count, 0)
                raise RuntimeError('rollback')
        self.assertEqual((await self.page())['total'], 0)
        self.assertEqual(self.emit.call_count, 0)

    async def test_savepoints_log_only_outer_commit_and_keep_unrelated_events(self):
        async with self.tx() as db:
            await attention.report(db, self.payload(1, scope='outer'))
            with self.assertRaises(RuntimeError):
                async with db.begin_nested():
                    await attention.report(db, self.payload(1, scope='rollback'))
                    raise RuntimeError('savepoint rollback')
            async with db.begin_nested():
                await attention.report(db, self.payload(1, scope='inner'))
            self.assertEqual(self.emit.call_count, 0)
        self.assertEqual(self.emit.call_count, 2)
        self.assertEqual((await self.page())['total'], 2)
        with self.assertRaises(RuntimeError):
            async with self.tx() as db:
                async with db.begin_nested():
                    await attention.report(db, self.payload(2, scope='inner'))
                raise RuntimeError('outer rollback')
        self.assertEqual(self.emit.call_count, 2)
        self.assertEqual(next(i for i in (await self.page())['items'] if i['scope']=='inner')['occurrences'], 1)

    async def test_registry_supports_another_service_internal_link_and_pagination(self):
        class InvoicePayload(BaseModel):
            model_config = ConfigDict(extra='forbid', strict=True)
            invoice_id: int
        definition = attention.Definition('invoices', 'error', InvoicePayload, lambda scope, data: f"/invoices/{data['invoice_id']}")
        with patch.dict(attention.DEFINITIONS, {'invoice.test_failed': definition}):
            for number in range(1, 4):
                async with self.tx() as db:
                    await attention.report(db, AttentionEvent(kind='invoice.test_failed', scope=str(number), sequence=1,
                        active=True, details={'invoice_id': number}))
            await self.publish(1)
            result = await self.page(source='invoices', limit=2)
            self.assertEqual((result['total'], len(result['items'])), (3, 2))
            self.assertEqual(result['items'][0]['link'], '/invoices/3')
            self.assertIn('attention_id=', result['items'][0]['log_link'])
            last = await self.page(source='invoices', limit=2, offset=2)
            self.assertEqual(last['items'][0]['scope'], '1')
        self.assertNotIn('invoice_id', str(self.emit.call_args_list), 'Business payloads do not enter the common log')
