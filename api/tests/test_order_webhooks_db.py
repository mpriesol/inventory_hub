"""Durable coalescing, crash recovery and current-state inbox integration."""
from current_schema_fixture import apply_current_core_schema
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import os
import asyncio
import base64
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.db_models import Shop, Warehouse
from inventory_hub.db_models_ext import StockMovement, Reservation
from inventory_hub.order_collection_models import OrderCollectionSettings, OrderInboxEntry
from inventory_hub.order_stock_models import OrderStockPolicy
from inventory_hub.order_processing_models import OrderProcessingJob
from inventory_hub.order_webhook_models import OrderWebhookSettings, OrderWebhookEvent
from inventory_hub.order_webhook_types import WebhookConfigure
from inventory_hub.services import order_webhooks as service, order_collection as collection
from inventory_hub.settings import settings

TEST_URL = os.environ.get('CATALOG_TEST_DATABASE_URL', '')
NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
START = NOW - timedelta(days=10)


def hint(number='A', event='update', offset=0):
    field = {'create': 'creation_time', 'update': 'last_update_time', 'delete': 'deletion_time'}[event]
    return service.notifications({'orders': [{'order_number': number, field: (NOW + timedelta(seconds=offset)).isoformat()}]}, event)


def entry(number='A', **changes):
    return {'order_number': number, 'uuid': str(uuid4()), 'created_at': (START + timedelta(days=1)).isoformat(),
            'updated_at': NOW.isoformat(), 'deleted': False, 'origin': 'frontend', 'status_id': 1, **changes}


@unittest.skipUnless(TEST_URL, 'Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB')
class OrderWebhookDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ('localhost', '127.0.0.1') or not parsed.path.endswith('_catalog_test'):
            raise RuntimeError('Webhook tests require a dedicated localhost *_catalog_test database')
        self.schema = 'order_webhook_test_' + uuid4().hex
        root = Path(__file__).resolve().parents[2] / 'infra' / 'db-init'
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            await connection.execute((root / '001_schema.sql').read_text())
            await connection.execute("CREATE TYPE payment_status AS ENUM ('unpaid', 'partial', 'paid')")
            for name in ('002_invoice_management.sql', '007_order_stock.sql', '008_order_collection.sql',
                         '009_stock_automation.sql', '011_fifo.sql', '035_order_webhooks.sql'):
                await connection.execute((root / name).read_text())
            await apply_current_core_schema(connection)
            # A repeated production upgrade preserves rows/constraints.
            await connection.execute((root / '035_order_webhooks.sql').read_text())
        finally:
            await connection.close()
        self.engine = create_async_engine(TEST_URL.replace('postgresql://', 'postgresql+asyncpg://', 1), poolclass=NullPool,
            connect_args={'server_settings': {'search_path': self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patches = [patch.object(service, 'now', return_value=NOW), patch.object(collection, 'now', return_value=NOW),
            patch.object(collection, 'target_fingerprint', return_value='a' * 64),
            patch.object(settings, 'SUPPLIER_CREDENTIALS_ROOT', Path(self.tmp.name) / 'private'),
            patch.object(settings, 'INVENTORY_DATA_ROOT', Path(self.tmp.name) / 'public')]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        async with self.transaction() as db:
            shop = await db.scalar(select(Shop).where(Shop.code == 'biketrek'))
            self.shop_id = shop.id
            warehouse = Warehouse(code='webhook-central', name='Webhook test')
            db.add(warehouse)
            await db.flush()
            db.add(OrderStockPolicy(shop_id=shop.id, warehouse_id=warehouse.id, starts_at=START,
                revision=1, status_actions={}, status_hash='a' * 64, statuses=[]))
            db.add(OrderCollectionSettings(shop_id=shop.id, enabled=True, revision=1, target_fingerprint='a' * 64,
                cursor_at=START, reconcile_cursor_at=START, next_poll_at=NOW, last_completed_at=START))
            db.add(OrderWebhookSettings(shop_id=shop.id, enabled=True, revision=1, target_fingerprint='a' * 64,
                secret_hash='b' * 64, public_base_url='https://hub.example.test/api', catchup_interval_seconds=3600,
                registration_status='registered', registered_events={'create': 1, 'update': 2, 'delete': 3},
                registered_at=START, registration_checked_at=NOW, next_registration_at=NOW + timedelta(hours=1)))

    async def asyncTearDown(self):
        try:
            async with self.transaction() as db:
                self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)), 0)
                self.assertEqual(await db.scalar(select(func.count()).select_from(Reservation)), 0)
        finally:
            await self.engine.dispose()
            connection = await asyncpg.connect(TEST_URL)
            try:
                await connection.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
            finally:
                await connection.close()

    @asynccontextmanager
    async def transaction(self):
        async with self.sessions() as db:
            async with db.begin():
                yield db

    async def accept(self, rows):
        async with self.transaction() as db:
            await service.accept(db, await db.get(Shop, self.shop_id), await db.get(OrderWebhookSettings, self.shop_id), rows)

    async def claim(self):
        async with self.transaction() as db:
            return await service.start_batch(db)

    async def test_older_delivery_ignored_but_identical_completed_hint_requires_new_read(self):
        await self.accept(hint())
        plan = await self.claim()
        current = entry()
        async with self.transaction() as db:
            await service.finish_batch(db, plan, entries={'A': current})
        await self.accept(hint(offset=-60))
        async with self.transaction() as db:
            row = await db.get(OrderWebhookEvent, (self.shop_id, 'A'))
            self.assertEqual((row.generation, row.status, row.attempts), (1, 'completed', 1))
            saved = await db.scalar(select(OrderInboxEntry))
            self.assertEqual(saved.source_uuid, current['uuid'])
            collector = await db.get(OrderCollectionSettings, self.shop_id)
            self.assertEqual(collector.cursor_at, START)
            self.assertEqual(collector.last_completed_at, START)
            self.assertEqual((await service.delivery_health(db, self.shop_id))['pending_count'], 0)
        self.assertIsNone(await self.claim())
        await self.accept(hint())
        await self.accept(hint())  # Coalesce while the new read is still pending.
        async with self.transaction() as db:
            row = await db.get(OrderWebhookEvent, (self.shop_id, 'A'))
            self.assertEqual((row.generation, row.status), (2, 'pending'))

    async def test_new_generation_survives_stale_inflight_completion(self):
        await self.accept(hint())
        old_plan = await self.claim()
        await self.accept(hint(offset=1))
        async with self.transaction() as db:
            await service.finish_batch(db, old_plan, entries={'A': entry()})
            row = await db.get(OrderWebhookEvent, (self.shop_id, 'A'))
            self.assertEqual((row.generation, row.status), (2, 'pending'))
            self.assertEqual(await db.scalar(select(func.count()).select_from(OrderInboxEntry)), 0)
        plan = await self.claim()
        async with self.transaction() as db:
            await service.finish_batch(db, plan, entries={'A': entry(updated_at=(NOW + timedelta(seconds=1)).isoformat())})
            self.assertEqual((await db.get(OrderWebhookEvent, (self.shop_id, 'A'))).status, 'completed')

    async def test_crash_requeues_read_and_missing_order_retains_visible_work(self):
        await self.accept(hint())
        await self.claim()
        async with self.transaction() as db:
            await service.recover(db)
            self.assertEqual((await db.get(OrderWebhookEvent, (self.shop_id, 'A'))).status, 'pending')
        plan = await self.claim()
        async with self.transaction() as db:
            await service.finish_batch(db, plan, entries={})
            row = await db.get(OrderWebhookEvent, (self.shop_id, 'A'))
            self.assertEqual(row.status, 'retry')
            self.assertEqual(row.last_error, 'order_webhook_order_not_found')
            self.assertEqual((await service.delivery_health(db, self.shop_id))['pending_count'], 1)
            self.assertEqual(await db.scalar(select(func.count()).select_from(OrderInboxEntry)), 0)

    async def test_deleted_membership_with_older_timestamp_is_review_never_restore(self):
        current = entry()
        await self.accept(hint())
        plan = await self.claim()
        async with self.transaction() as db:
            await service.finish_batch(db, plan, entries={'A': current})
        await self.accept(hint(event='delete', offset=1))
        plan = await self.claim()
        async with self.transaction() as db:
            await service.finish_batch(db, plan, entries={'A': {**current, 'deleted': True,
                'updated_at': (NOW - timedelta(seconds=1)).isoformat()}})
            inbox = await db.scalar(select(OrderInboxEntry))
            self.assertTrue(inbox.deleted)
            self.assertEqual(inbox.review_reason, 'order_collection_deleted')
            self.assertEqual((await db.get(OrderWebhookEvent, (self.shop_id, 'A'))).status, 'completed')

    async def test_pause_does_not_hide_known_pending_notifications(self):
        await self.accept(hint())
        async with self.transaction() as db:
            row = await db.get(OrderWebhookSettings, self.shop_id)
            row.enabled = False
            await db.flush()
            health = await service.delivery_health(db, self.shop_id)
            self.assertFalse(health['enabled'])
            self.assertEqual(health['pending_count'], 1)
        self.assertIsNone(await self.claim())

    async def test_registration_configuration_does_not_enable_collector_or_stock(self):
        async with self.transaction() as db:
            collector = await db.get(OrderCollectionSettings, self.shop_id)
            collector.enabled = False
        async with self.sessions() as db:
            with self.assertRaises(service.WebhookError) as raised:
                await service.configure(db, WebhookConfigure(shop_code='biketrek', confirmed=True,
                    expected_revision=1, enabled=True, public_base_url='https://hub.example.test/api'))
            self.assertEqual(raised.exception.code, 'order_webhook_collection_required')
            await db.rollback()
        async with self.transaction() as db:
            self.assertFalse((await db.get(OrderCollectionSettings, self.shop_id)).enabled)

    async def test_effective_interval_uses_verified_registration_and_preserves_polling_fallback(self):
        async with self.transaction() as db:
            self.assertEqual(await service.effective_poll_interval(db, self.shop_id, 300), 3600)
            row = await db.get(OrderWebhookSettings, self.shop_id)
            row.registration_status = 'failed'
            await db.flush()
            self.assertEqual(await service.effective_poll_interval(db, self.shop_id, 300), 300)

    async def test_successful_registration_schedules_coverage_after_activation(self):
        async with self.transaction() as db:
            row = await db.get(OrderWebhookSettings, self.shop_id)
            row.registration_status, row.registered_at = 'pending', None
            row.next_registration_at = NOW
            row.registered_events = {}
            await db.flush()
            plan = await service.start_registration(db)
        async with self.transaction() as db:
            await service.finish_registration(db, plan, events={'create': 1, 'update': 2, 'delete': 3})
            collector = await db.get(OrderCollectionSettings, self.shop_id)
            self.assertEqual(collector.next_poll_at, NOW)
            self.assertEqual(collector.last_mode, 'reconcile')
            self.assertEqual(collector.cursor_at, START)
            self.assertEqual((await db.get(OrderWebhookSettings, self.shop_id)).registered_at, NOW)

    async def test_paused_oldest_shop_does_not_starve_another_shop(self):
        await self.accept(hint())
        async with self.transaction() as db:
            (await db.get(OrderCollectionSettings, self.shop_id)).enabled = False
            shop = await db.scalar(select(Shop).where(Shop.code == 'xtrek'))
            other_id = shop.id
            db.add(OrderCollectionSettings(shop_id=other_id, enabled=True, revision=1, target_fingerprint='a' * 64,
                cursor_at=START, reconcile_cursor_at=START, next_poll_at=NOW))
            webhook = OrderWebhookSettings(shop_id=other_id, enabled=True, revision=1, target_fingerprint='a' * 64,
                secret_hash='c' * 64, public_base_url='https://hub.example.test/api', catchup_interval_seconds=3600,
                registration_status='registered', registered_events={}, next_registration_at=NOW + timedelta(hours=1))
            db.add(webhook)
            await db.flush()
            await service.accept(db, shop, webhook, hint('B'))
        plan = await self.claim()
        self.assertEqual(plan['shop_id'], other_id)
        self.assertEqual([row['order_number'] for row in plan['events']], ['B'])
        async with self.transaction() as db:
            self.assertEqual((await db.get(OrderWebhookEvent, (self.shop_id, 'A'))).status, 'pending')

    async def test_changed_target_is_fenced_without_starving_another_shop(self):
        await self.accept(hint())
        async with self.transaction() as db:
            shop = await db.scalar(select(Shop).where(Shop.code == 'xtrek'))
            other_id = shop.id
            db.add(OrderCollectionSettings(shop_id=other_id, enabled=True, revision=1, target_fingerprint='a' * 64,
                cursor_at=START, reconcile_cursor_at=START, next_poll_at=NOW))
            webhook = OrderWebhookSettings(shop_id=other_id, enabled=True, revision=1, target_fingerprint='a' * 64,
                secret_hash='c' * 64, public_base_url='https://hub.example.test/api', catchup_interval_seconds=3600,
                registration_status='registered', registered_events={}, next_registration_at=NOW + timedelta(hours=1))
            db.add(webhook)
            await db.flush()
            await service.accept(db, shop, webhook, hint('B'))
        with patch.object(collection, 'target_fingerprint', side_effect=lambda code: 'c' * 64 if code == 'biketrek' else 'a' * 64):
            self.assertIsNone(await self.claim())  # Fence commits before another shop is locked.
            plan = await self.claim()
        self.assertEqual(plan['shop_id'], other_id)
        async with self.transaction() as db:
            row = await db.get(OrderWebhookEvent, (self.shop_id, 'A'))
            self.assertEqual((row.status, row.last_error), ('review', 'order_webhook_target_changed'))

    async def test_ingress_and_worker_use_shop_before_settings_lock_order(self):
        token = 'x' * 64
        auth = 'Basic ' + base64.b64encode(('biketrek:' + token).encode()).decode()
        async with self.transaction() as db:
            (await db.get(OrderWebhookSettings, self.shop_id)).secret_hash = service.credentials.digest(token)
        async def receive():
            async with self.transaction() as db:
                shop, row = await service.authenticate(db, auth)
                await service.accept(db, shop, row, hint())
        task = None
        try:
            async with self.transaction() as db:
                await db.scalar(select(Shop).where(Shop.id == self.shop_id).with_for_update())
                task = asyncio.create_task(receive())
                await asyncio.sleep(0.05)
                # With the old settings->Shop-FK order this would wait for the
                # ingress transaction while ingress waited for this Shop lock.
                async with asyncio.timeout(0.2):
                    await db.get(OrderWebhookSettings, self.shop_id, with_for_update=True)
            await asyncio.wait_for(task, timeout=2)
        finally:
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        async with self.transaction() as db:
            self.assertIsNotNone(await db.get(OrderWebhookEvent, (self.shop_id, 'A')))

    async def test_same_second_notification_wakes_existing_full_order_job(self):
        current = entry()
        await self.accept(hint())
        plan = await self.claim()
        async with self.transaction() as db:
            await service.finish_batch(db, plan, entries={'A': current})
            inbox = await db.scalar(select(OrderInboxEntry))
            db.add(OrderProcessingJob(shop_id=self.shop_id, inbox_id=inbox.id, source_uuid=inbox.source_uuid,
                order_number='A', observation_hash=inbox.observation_hash, generation=1, status='running',
                next_attempt_at=NOW + timedelta(days=1), attempts=1))
        await self.accept(hint())  # Same timestamp/status can be a second quantity edit.
        plan = await self.claim()
        async with self.transaction() as db:
            await service.finish_batch(db, plan, entries={'A': current})
            job = await db.scalar(select(OrderProcessingJob))
            self.assertEqual((job.generation, job.status, job.next_attempt_at), (2, 'pending', NOW))
            inbox = await db.scalar(select(OrderInboxEntry))
            self.assertEqual(job.observation_hash, inbox.observation_hash)

    async def test_create_intent_survives_restart_and_is_cleared_only_when_verified(self):
        async with self.transaction() as db:
            row = await db.get(OrderWebhookSettings, self.shop_id)
            row.registration_status, row.next_registration_at = 'pending', NOW
            await db.flush()
            plan = await service.start_registration(db)
        async with self.transaction() as db:
            await service.record_create_intent(db, plan, 'create')
        async with self.transaction() as db:
            await service.recover(db)
            row = await db.get(OrderWebhookSettings, self.shop_id)
            self.assertIn('create', row.registration_intents)
            next_plan = await service.start_registration(db)
            self.assertIn('create', next_plan['registration_intents'])
        async with self.transaction() as db:
            await service.finish_registration(db, next_plan, events={'create': 10, 'update': 11, 'delete': 12})
            row = await db.get(OrderWebhookSettings, self.shop_id)
            self.assertEqual(row.registration_intents, {})
            self.assertEqual(row.registration_status, 'registered')
