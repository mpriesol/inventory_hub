"""Real durable discovery evidence; no transport or stock mutations."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.db_models import Shop
from inventory_hub.order_collection_models import OrderCollectionRun, OrderCollectionSettings
from inventory_hub.order_webhook_models import OrderWebhookEvent, OrderWebhookSettings
from inventory_hub.services.order_readiness import collection_blockers


TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")
NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
TARGET = "a" * 64


@unittest.skipUnless(TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class OrderReadinessDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("Readiness tests require a dedicated localhost *_catalog_test database")
        self.schema = "order_readiness_" + uuid4().hex
        root = Path(__file__).resolve().parents[2] / "infra" / "db-init"
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            await connection.execute((root / "001_schema.sql").read_text())
            await connection.execute("CREATE TYPE payment_status AS ENUM ('unpaid','partial','paid')")
            for filename in ("002_invoice_management.sql", "007_order_stock.sql", "008_order_collection.sql",
                             "009_stock_automation.sql", "035_order_webhooks.sql"):
                await connection.execute((root / filename).read_text())
        finally:
            await connection.close()
        self.engine = create_async_engine(TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1),
            poolclass=NullPool, connect_args={"server_settings": {"search_path": self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        async with self.sessions.begin() as db:
            self.shop_id = await db.scalar(select(Shop.id).where(Shop.code == "biketrek"))
            db.add(OrderCollectionSettings(shop_id=self.shop_id, enabled=True, revision=2,
                target_fingerprint=TARGET, cursor_at=NOW - timedelta(minutes=40),
                reconcile_cursor_at=NOW - timedelta(days=1), last_completed_at=NOW - timedelta(minutes=39),
                next_poll_at=NOW + timedelta(minutes=20)))
            db.add(OrderWebhookSettings(shop_id=self.shop_id, enabled=True, revision=1,
                target_fingerprint=TARGET, secret_hash="b" * 64, public_base_url="https://hub-test.invalid",
                catchup_interval_seconds=3600, registration_status="registered", registered_events={},
                registered_at=NOW - timedelta(days=1), registration_checked_at=NOW - timedelta(minutes=10),
                next_registration_at=NOW + timedelta(minutes=50)))
            db.add(self.make_run())

    def make_run(self, *, mode="delta", revision=2, minutes=40):
        started = NOW - timedelta(minutes=minutes)
        return OrderCollectionRun(id=str(uuid4()), shop_id=self.shop_id, settings_revision=revision,
            mode=mode, status="completed", started_at=started, completed_at=started + timedelta(seconds=30),
            from_at=started - timedelta(hours=1), until_at=started,
            configuration_snapshot={"run_timeout_seconds": 180})

    async def asyncTearDown(self):
        await self.engine.dispose()
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            await connection.close()

    async def check(self, db):
        return await collection_blockers(db, self.shop_id, TARGET, 900, at=NOW)

    async def test_current_revision_completed_delta_is_not_replaced_by_reconciliation(self):
        async with self.sessions.begin() as db:
            db.add(self.make_run(mode="reconcile", minutes=2))
            db.add(self.make_run(revision=1, minutes=1))
            await db.flush()
            self.assertEqual(await self.check(db), [])
            collector = await db.get(OrderCollectionSettings, self.shop_id)
            collector.revision = 3
            collector.cursor_at = collector.last_completed_at = NOW
            await db.flush()
            self.assertEqual(await self.check(db), ["stock_sync_orders_stale"])

    async def test_durable_known_event_blocks_after_disable_until_consumed(self):
        async with self.sessions.begin() as db:
            collector = await db.get(OrderCollectionSettings, self.shop_id)
            collector.cursor_at = collector.last_completed_at = NOW
            webhook = await db.get(OrderWebhookSettings, self.shop_id)
            webhook.enabled = False
            event = OrderWebhookEvent(shop_id=self.shop_id, order_number="READINESS-01", event="update",
                notification_at=NOW, notification_hash="c" * 64, generation=1, status="pending",
                next_attempt_at=NOW, last_received_at=NOW)
            db.add(event)
            for state in ("pending", "running", "retry", "review"):
                event.status = state
                await db.flush()
                self.assertEqual(await self.check(db), ["stock_sync_order_notifications_pending"])
            event.status = "completed"
            await db.flush()
            self.assertEqual(await self.check(db), [])

    async def test_repeated_readiness_reloads_changed_registration_in_same_session(self):
        async with self.sessions() as db:
            # Keep the ORM instance alive to expose accidental identity-map reuse.
            original = await db.get(OrderWebhookSettings, self.shop_id)
            self.assertEqual(await self.check(db), [])
            async with self.sessions.begin() as other:
                row = await other.get(OrderWebhookSettings, self.shop_id)
                row.registration_status = "failed"
                row.last_error = "registration_failed"
            self.assertEqual(await self.check(db), ["stock_sync_order_webhook_not_ready"])
            self.assertEqual(original.registration_status, "failed")
