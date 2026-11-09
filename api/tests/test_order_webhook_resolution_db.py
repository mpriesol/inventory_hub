"""Real PostgreSQL proof, CAS, replay and immutable notification decisions."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4
import asyncio
import unittest

import asyncpg
from sqlalchemy import func, select

import test_order_webhooks_db as fixture
from inventory_hub.db_models import MovementType, OrderStatus, Product
from inventory_hub.db_models_ext import Reservation, ShopOrder, StockMovement, SyncLog
from inventory_hub.order_collection_models import OrderCollectionSettings, OrderInboxEntry
from inventory_hub.order_processing_models import OrderProcessingJob
from inventory_hub.order_webhook_models import OrderWebhookEvent, OrderWebhookSettings
from inventory_hub.order_webhook_types import NotificationResolutionApply, NotificationResolutionPreview
from inventory_hub.services import order_webhooks, order_webhook_resolution as service
from inventory_hub.services.order_collection_source import CollectionSourceError


@unittest.skipUnless(fixture.TEST_URL, 'Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB')
class NotificationResolutionDatabaseTests(unittest.IsolatedAsyncioTestCase):
    transaction = fixture.OrderWebhookDatabaseTests.transaction

    async def asyncSetUp(self):
        await fixture.OrderWebhookDatabaseTests.asyncSetUp(self)
        connection = await asyncpg.connect(fixture.TEST_URL)
        try:
            await connection.execute(f'SET search_path TO "{self.schema}"')
            await connection.execute((Path(__file__).resolve().parents[2] / 'infra/db-init/025_stock_native.sql').read_text())
        finally:
            await connection.close()
        async with self.transaction() as db:
            db.add(OrderWebhookEvent(shop_id=self.shop_id, order_number='MISSING', event='delete',
                notification_at=fixture.NOW, notification_hash='d' * 64, generation=1,
                status='review', attempts=8, next_attempt_at=fixture.NOW, last_received_at=fixture.NOW,
                last_error='order_webhook_order_not_found'))
        self.upstream = AsyncMock(return_value={})
        self.read_patch = patch.object(order_webhooks, 'fetch_current', self.upstream)
        self.read_patch.start()
        self.addCleanup(self.read_patch.stop)

    async def asyncTearDown(self):
        await self.engine.dispose()
        connection = await asyncpg.connect(fixture.TEST_URL)
        try:
            await connection.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            await connection.close()

    def request(self, **changes):
        return NotificationResolutionPreview(**{'request_id': uuid4(), 'shop_code': 'biketrek',
            'order_number': 'MISSING', 'action': service.ACKNOWLEDGE,
            'note': 'Checked both sources and no Hub order or stock history.', **changes})

    async def prepare(self, request=None, actor='user:1'):
        async with self.sessions() as db:
            return await service.preview(db, request or self.request(), actor)

    async def apply(self, operation, actor='user:1', **changes):
        payload = NotificationResolutionApply(**{'shop_code': operation['shop_code'],
            'preview_hash': operation['preview_hash'], 'confirmed': True, **changes})
        async with self.sessions() as db:
            return await service.apply(db, operation['request_id'], payload, actor)

    async def audit(self):
        async with self.sessions() as db:
            return [(row.id, row.status, deepcopy(row.metadata_)) for row in
                (await db.scalars(select(SyncLog).order_by(SyncLog.id))).all()]

    async def assert_error(self, awaitable, code):
        with self.assertRaises(order_webhooks.WebhookError) as raised:
            await awaitable
        self.assertEqual(raised.exception.code, 'order_webhook_resolution_' + code)

    async def add_order(self, db):
        order = ShopOrder(shop_id=self.shop_id, external_id='MISSING', external_code='MISSING',
            order_date=fixture.NOW, status=OrderStatus.new, currency=None, stock_state='unmanaged', stock_revision=0)
        db.add(order)
        await db.flush()
        return order

    async def add_inbox(self, db, number='MISSING'):
        row = OrderInboxEntry(shop_id=self.shop_id, source_uuid=str(uuid4()), order_number=number,
            created_at=fixture.NOW, updated_at=fixture.NOW, deleted=True, origin='admin', status_id=1,
            observation_hash='e' * 64, observed_at=fixture.NOW, last_seen_at=fixture.NOW,
            review_reason='order_collection_deleted')
        db.add(row)
        await db.flush()
        return row

    async def test_acknowledge_reopen_and_replay_preserve_original_and_append_audit(self):
        request = self.request()
        prepared = await self.prepare(request)
        first = await self.audit()
        self.assertEqual(len(first), 1)
        self.assertEqual(prepared['phase'], 'prepared')
        self.assertIsNone(prepared['result'])
        self.assertEqual(await self.prepare(request), prepared)
        completed = await self.apply(prepared)
        self.assertEqual(completed['result']['event_status'], 'completed')
        self.assertEqual(await self.apply(prepared), completed)
        self.assertEqual(await self.prepare(request), completed)
        self.assertEqual(self.upstream.await_count, 2)
        completed_audit = await self.audit()
        async with self.sessions() as db:
            self.assertEqual(await service.get_operation(db, prepared['request_id'], 'biketrek'), completed)
            status = await order_webhooks.status(db, 'biketrek')
            self.assertEqual(status['review_count'], 0)
            self.assertEqual(status['events'][0]['resolution']['request_id'], prepared['request_id'])
            row = await db.get(OrderWebhookEvent, (self.shop_id, 'MISSING'))
            self.assertEqual((row.event, row.generation, row.notification_hash, row.attempts), ('delete', 1, 'd' * 64, 8))
            self.assertEqual(row.notification_at, fixture.NOW)
            self.assertIsNone((await db.get(OrderWebhookSettings, self.shop_id)).last_completed_at)
            collector = await db.get(OrderCollectionSettings, self.shop_id)
            self.assertEqual((collector.cursor_at, collector.last_completed_at), (fixture.START, fixture.START))
        reopen = await self.prepare(self.request(action='reopen', note='Return this manual decision to operational review.'))
        reopened = await self.apply(reopen)
        self.assertEqual(reopened['result']['event_status'], 'review')
        self.assertEqual(self.upstream.await_count, 2)  # Reopening has no upstream effect or probe.
        self.assertEqual((await self.audit())[:1], first)
        self.assertEqual((await self.audit())[:2], completed_audit)
        self.assertEqual(len(await self.audit()), 4)
        async with self.sessions() as db:
            row = await db.get(OrderWebhookEvent, (self.shop_id, 'MISSING'))
            self.assertEqual((row.generation, row.status, row.last_error), (1, 'review', 'order_webhook_order_not_found'))
            self.assertIsNone(row.last_completed_at)
            history = await service.history(db, 'biketrek', 'MISSING')
            self.assertEqual([x['action'] for x in history['operations']], ['reopen', service.ACKNOWLEDGE])
            for model in (ShopOrder, OrderInboxEntry, OrderProcessingJob, StockMovement, Reservation):
                self.assertEqual(await db.scalar(select(func.count()).select_from(model)), 0)
        again = await self.prepare()
        await self.apply(again)
        self.assertEqual(len(await self.audit()), 6)

    async def test_source_failure_and_found_order_never_store_preview_or_complete(self):
        self.upstream.side_effect = CollectionSourceError('order_collection_invalid_response')
        with self.assertRaises(CollectionSourceError):
            await self.prepare()
        self.assertEqual(await self.audit(), [])
        self.upstream.side_effect = None
        self.upstream.return_value = {'MISSING': {'deleted': True}}
        await self.assert_error(self.prepare(), 'source_exists')
        self.assertEqual(await self.audit(), [])
        self.upstream.return_value = {}
        prepared = await self.prepare()
        self.upstream.side_effect = CollectionSourceError('order_collection_rate_limited', 429)
        with self.assertRaises(CollectionSourceError):
            await self.apply(prepared)
        self.assertEqual(len(await self.audit()), 1)
        async with self.sessions() as db:
            self.assertEqual((await db.get(OrderWebhookEvent, (self.shop_id, 'MISSING'))).status, 'review')

    async def test_unmanaged_order_before_preview_blocks_without_source_read(self):
        async with self.transaction() as db:
            await self.add_order(db)
        await self.assert_error(self.prepare(), 'local_history')
        self.upstream.assert_not_awaited()
        self.assertEqual(await self.audit(), [])

    async def test_new_local_order_after_preview_blocks_apply(self):
        prepared = await self.prepare()
        async with self.transaction() as db:
            await self.add_order(db)
        await self.assert_error(self.apply(prepared), 'local_history')
        self.assertEqual(self.upstream.await_count, 1)
        self.assertEqual(len(await self.audit()), 1)

    async def test_any_inbox_or_processing_history_blocks(self):
        async with self.transaction() as db:
            inbox = await self.add_inbox(db, 'OTHER')
            db.add(OrderProcessingJob(shop_id=self.shop_id, inbox_id=inbox.id, source_uuid=inbox.source_uuid,
                order_number='MISSING', observation_hash='e' * 64, generation=1, status='review',
                next_attempt_at=fixture.NOW, attempts=0, created_at=fixture.NOW, updated_at=fixture.NOW))
        await self.assert_error(self.prepare(), 'local_history')
        self.upstream.assert_not_awaited()
        async with self.transaction() as db:
            await self.add_inbox(db)
        await self.assert_error(self.prepare(), 'local_history')

    async def test_orphan_movement_keeps_review_even_without_local_order(self):
        async with self.transaction() as db:
            from inventory_hub.order_stock_models import OrderStockPolicy
            policy = await db.get(OrderStockPolicy, self.shop_id)
            product = Product(sku='ORPHAN', name='Orphan proof')
            db.add(product)
            await db.flush()
            db.add(StockMovement(idempotency_key='test-orphan', product_id=product.id, warehouse_id=policy.warehouse_id,
                movement_type=MovementType.SALE_OUT, quantity=Decimal('-1'), unit_cost=None, total_cost=None,
                unit_cost_currency='EUR', fx_rate_to_eur=Decimal('1'), reference_type='shop_order',
                reference_id='987', reference_source=str(self.shop_id), balance_after=Decimal('0'),
                avg_cost_after=None, created_by='fixture'))
        await self.assert_error(self.prepare(), 'local_history')
        self.upstream.assert_not_awaited()
        self.assertEqual(await self.audit(), [])

    async def test_new_generation_during_source_read_is_not_acknowledged_and_no_locks_span_network(self):
        prepared = await self.prepare()
        async def changed(_plan):
            async with self.transaction() as db:
                # NOWAIT proves the preparation transaction released Shop.
                from inventory_hub.db_models import Shop
                await db.scalar(select(Shop).where(Shop.id == self.shop_id).with_for_update(nowait=True))
                row = await db.get(OrderWebhookEvent, (self.shop_id, 'MISSING'))
                row.generation += 1
                row.notification_hash = 'f' * 64
            return {}
        self.upstream.side_effect = changed
        await self.assert_error(self.apply(prepared), 'stale')
        self.assertEqual(len(await self.audit()), 1)

    async def test_preview_refuses_local_history_created_during_read(self):
        async def changed(_plan):
            async with self.transaction() as db:
                await self.add_inbox(db)
            return {}
        self.upstream.side_effect = changed
        await self.assert_error(self.prepare(), 'local_history')
        self.assertEqual(await self.audit(), [])

    async def test_settings_target_expiry_actor_and_payload_are_bound(self):
        request = self.request()
        prepared = await self.prepare(request)
        await self.assert_error(self.prepare(request.model_copy(update={'note': 'Another valid explanation entirely.'})), 'request_conflict')
        await self.assert_error(self.apply(prepared, actor='user:2'), 'request_conflict')
        await self.assert_error(self.apply(prepared, preview_hash='b' * 64), 'request_conflict')
        await self.assert_error(self.apply(prepared, shop_code='xtrek'), 'request_conflict')
        with patch.object(order_webhooks, 'now', return_value=fixture.NOW + timedelta(minutes=16)):
            await self.assert_error(self.apply(prepared), 'expired')
        with patch.object(service.collection, 'target_fingerprint', return_value='c' * 64):
            await self.assert_error(self.apply(prepared), 'target_changed')
        async with self.transaction() as db:
            (await db.get(OrderWebhookSettings, self.shop_id)).revision += 1
        await self.assert_error(self.apply(prepared), 'stale')
        self.assertEqual(self.upstream.await_count, 1)

    async def test_same_uuid_concurrent_apply_has_one_completed_journal_entry(self):
        prepared = await self.prepare()
        results = await asyncio.gather(self.apply(prepared), self.apply(prepared))
        self.assertEqual(results[0], results[1])
        self.assertEqual([row[1] for row in await self.audit()], ['prepared', 'completed'])

    async def test_distinct_previews_cannot_complete_same_event_twice(self):
        first, second = await asyncio.gather(self.prepare(), self.prepare())
        results = await asyncio.gather(self.apply(first), self.apply(second), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(sum(isinstance(result, order_webhooks.WebhookError) for result in results), 1)
        self.assertEqual(sum(row[1] == 'completed' for row in await self.audit()), 1)

    async def test_reopen_cannot_change_new_notification_generation(self):
        completed = await self.apply(await self.prepare())
        reopen = await self.prepare(self.request(action='reopen', note='Return this manual decision to review.'))
        async with self.transaction() as db:
            row = await db.get(OrderWebhookEvent, (self.shop_id, 'MISSING'))
            row.generation += 1
            row.status = 'pending'
        await self.assert_error(self.apply(reopen), 'not_eligible')
        async with self.sessions() as db:
            status = await order_webhooks.status(db, 'biketrek')
            self.assertNotIn('resolution', status['events'][0])
            self.assertEqual(status['pending_count'], 1)
            self.assertEqual(await service.get_operation(db, completed['request_id'], 'biketrek'), completed)

    async def test_intervening_ack_and_reopen_invalidates_older_same_state_preview(self):
        older = await self.prepare()
        await self.apply(await self.prepare())
        await self.apply(await self.prepare(self.request(action='reopen', note='Reopen the newer manual decision.')))
        # Status, generation, hash and original timestamps are back to exactly
        # their previous values; the immutable decision sequence still changed.
        await self.assert_error(self.apply(older), 'stale')
