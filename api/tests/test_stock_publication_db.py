"""Maintenance publication durability in disposable localhost PostgreSQL only."""
import copy
import asyncio
import os
import unittest
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from fastapi import HTTPException
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.db_models import MovementType, Product, ReceivingStatus, Shop, Supplier, Warehouse
from inventory_hub.db_models_ext import ReceivingLine, ReceivingSession, ShopOrder, ShopProduct, StockBalance, StockMovement
from inventory_hub.opening_stock_types import OpeningFinalizeRequest, OpeningPreviewRequest
from inventory_hub.order_stock_models import OrderStockPolicy
from inventory_hub.stock_settings_models import StockShopSettings, StockWarehouseSettings
from inventory_hub.stock_publication_models import (
    StockPublicationBatch, StockPublicationHold, StockPublicationItem, StockPublicationPolicy,
)
from inventory_hub.stock_publication_types import (
    StockPublicationConfigure, StockPublicationOpenHold, StockPublicationPreview,
    StockPublicationConfirmed, StockPublicationReleaseHold, StockPublicationResolve, StockPublicationSubmit,
)
from inventory_hub.services import order_collection as collection
from inventory_hub.services import stock_publication as service
from inventory_hub.services import stock_publication_source as source
from inventory_hub.services import stock_balances
from inventory_hub.services import opening_stock, order_stock_ledger
from inventory_hub.routers import receiving_db
from inventory_hub.services.stock_publication_gate import StockPublicationHoldError


TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")
NOW = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
D = Decimal


@unittest.skipUnless(TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class StockPublicationDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("Publication tests require a dedicated localhost *_catalog_test database")
        self.schema = "stock_publication_test_" + uuid4().hex
        sql_root = Path(__file__).resolve().parents[2] / "infra" / "db-init"
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            await connection.execute((sql_root / "001_schema.sql").read_text())
            await connection.execute("CREATE TYPE payment_status AS ENUM ('unpaid', 'partial', 'paid')")
            for filename in ("002_invoice_management.sql", "006_opening_stock.sql", "007_order_stock.sql", "008_order_collection.sql",
                             "009_stock_automation.sql", "010_stock_publication.sql"):
                await connection.execute((sql_root / filename).read_text())
        finally:
            await connection.close()
        self.engine = create_async_engine(TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1),
            poolclass=NullPool, connect_args={"server_settings": {"search_path": self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        self.config = {"upgates_api_base_url": "https://publication-test.invalid/api/v2",
                       "upgates_login": "synthetic-publisher", "upgates_api_key": "test-only-secret"}
        self.patchers = [patch.object(service, "now", return_value=NOW),
                         patch.object(collection, "load_shop", side_effect=lambda code: dict(self.config)),
                         patch.object(service.settings, "STOCK_PUBLICATION_WRITE_ENABLED", False),
                         patch.object(service.source, "read_stock", AsyncMock(side_effect=self.read_remote)),
                         patch.object(service.source, "write_stock_once", AsyncMock(side_effect=self.write_remote))]
        for patcher in self.patchers:
            patcher.start()
        self.read = service.source.read_stock
        self.write = service.source.write_stock_once
        self.network_session = ContextVar("publication_network_session", default=None)
        self.remote = {
            "SKU-A": {"identity": {"code": "SKU-A", "parent_code": "xTrek", "variant_code": "SKU-A",
                                   "product_id": 901, "variant_id": 902}, "quantity": "9"},
            "SKU-B": {"identity": {"code": "SKU-B", "parent_code": "SKU-B", "variant_code": None,
                                   "product_id": 903, "variant_id": None}, "quantity": "8"},
        }
        async with self.transaction() as db:
            self.shops = {row.code: row.id for row in (await db.scalars(select(Shop))).all()}
            warehouse = Warehouse(code="publication-central", name="Publication central")
            products = [Product(sku=sku, name=sku) for sku in self.remote]
            db.add_all([warehouse, *products])
            await db.flush()
            self.warehouse_id = warehouse.id
            self.products = {product.sku: product.id for product in products}
            db.add(OrderStockPolicy(shop_id=self.shops["biketrek"], warehouse_id=warehouse.id,
                starts_at=NOW - timedelta(days=30), revision=1, status_actions={}, status_hash="a" * 64, statuses=[]))
            db.add(StockWarehouseSettings(warehouse_id=warehouse.id, revision=1, values={},
                                         processing_paused=True, updated_at=NOW))
            db.add(StockShopSettings(shop_id=self.shops["biketrek"], revision=1, overrides={}, mode="manual", updated_at=NOW))
            for product in products:
                target = self.remote[product.sku]["identity"]
                db.add(ShopProduct(shop_id=self.shops["biketrek"], product_id=product.id,
                    external_code=target["parent_code"], parent_code=target["parent_code"],
                    variant_code=target["variant_code"], is_variant=target["variant_code"] is not None))
                db.add(StockBalance(product_id=product.id, warehouse_id=warehouse.id, qty_on_hand=D("7"),
                    qty_reserved=D("3"), avg_cost=D("3"), total_value=D("21")))
                db.add(StockMovement(idempotency_key=uuid4().hex, product_id=product.id, warehouse_id=warehouse.id,
                    movement_type=MovementType.INITIAL, quantity=D("7"), unit_cost=D("3"),
                    balance_after=D("7"), avg_cost_after=D("3")))

    async def asyncTearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        await self.engine.dispose()
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            await connection.close()

    @asynccontextmanager
    async def transaction(self):
        async with self.sessions() as db:
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def physical_snapshot(self):
        async with self.sessions() as db:
            return {table: (await db.execute(text(f"SELECT * FROM {table} ORDER BY id"))).mappings().all()
                    for table in ("stock_balances", "stock_movements", "reservations", "shop_orders",
                                  "shop_order_items", "shop_sync_outbox")}

    async def read_remote(self, shop_code, target, expected_target_fingerprint):
        self.assertIsNotNone(self.network_session.get())
        self.assertFalse(self.network_session.get().in_transaction(), "Remote reads must not retain database locks")
        self.assertEqual(shop_code, "biketrek")
        self.assertEqual(expected_target_fingerprint, collection.target_fingerprint(shop_code))
        observed = self.remote[target["code"]]
        for key in ("product_id", "variant_id"):
            if key in target and target[key] != observed["identity"][key]:
                raise source.SourceError("stock_publication_identity_changed", 409)
        return copy.deepcopy(observed)

    async def write_remote(self, shop_code, frozen_identity, quantity, expected_target_fingerprint):
        self.assertIsNotNone(self.network_session.get())
        self.assertFalse(self.network_session.get().in_transaction(), "Intent must commit before dispatching the PUT")
        self.assertEqual(shop_code, "biketrek")
        self.assertEqual(expected_target_fingerprint, collection.target_fingerprint(shop_code))
        self.assertEqual(frozen_identity, self.remote[frozen_identity["code"]]["identity"])
        self.remote[frozen_identity["code"]]["quantity"] = quantity
        return {"acknowledged": True}

    async def invoke(self, name, *args):
        async with self.sessions() as db:
            token = self.network_session.set(db)
            try:
                return await getattr(service, name)(db, *args)
            except BaseException:
                await db.rollback()
                raise
            finally:
                self.network_session.reset(token)

    async def configure(self, enabled=True, revision=0):
        return await self.invoke("configure", StockPublicationConfigure(shop_code="biketrek",
            expected_revision=revision, enabled=enabled, confirmed=True))

    async def open_hold(self):
        await self.invoke("open_hold", StockPublicationOpenHold(shop_code="biketrek", confirmed=True,
            external_writers_paused=True, orders_reconciled=True))
        async with self.sessions() as db:
            return await db.scalar(select(StockPublicationHold).where(StockPublicationHold.active.is_(True)))

    async def preview(self, skus=None, request_id=None):
        identifier = request_id or uuid4()
        await self.invoke("preview", StockPublicationPreview(request_id=identifier,
            shop_code="biketrek", skus=skus or ["SKU-A"]))
        async with self.sessions() as db:
            return await db.get(StockPublicationBatch, str(identifier))

    async def prepare(self, skus=None):
        await self.configure()
        await self.open_hold()
        return await self.preview(skus)

    async def submit(self, batch):
        return await self.invoke("submit", batch.id,
            StockPublicationSubmit(preview_hash=batch.preview_hash, confirmed=True))

    async def state(self, batch):
        async with self.sessions() as db:
            row = await db.get(StockPublicationBatch, batch.id)
            items = (await db.scalars(select(StockPublicationItem).where(StockPublicationItem.batch_id == batch.id)
                                      .order_by(StockPublicationItem.position))).all()
            hold = await db.get(StockPublicationHold, row.hold_id)
            return SimpleNamespace(batch=row, items=items, hold=hold)

    async def test_preview_is_read_only_idempotent_and_global_write_gate_defaults_off(self):
        before = await self.physical_snapshot()
        batch = await self.prepare()
        state = await self.state(batch)
        self.assertEqual((batch.status, state.items[0].quantity, state.items[0].before_quantity),
                         ("prepared", "4", "9"))
        self.assertEqual(state.items[0].target, self.remote["SKU-A"]["identity"])
        self.assertEqual(await self.physical_snapshot(), before)
        read_count = self.read.await_count
        same = await self.preview(request_id=batch.id)
        self.assertEqual((same.id, same.preview_hash), (batch.id, batch.preview_hash))
        self.assertEqual(self.read.await_count, read_count)
        with self.assertRaises(service.PublicationError):
            await self.preview(["SKU-B"], request_id=batch.id)
        with self.assertRaises(service.PublicationError) as raised:
            await self.submit(batch)
        self.assertEqual(raised.exception.code, "stock_publication_write_disabled")
        self.assertEqual((await self.state(batch)).batch.status, "prepared")
        self.write.assert_not_awaited()

    async def test_unknown_local_quantity_blocks_entire_batch_without_put(self):
        batch = await self.prepare(["SKU-A", "MISSING-SKU"])
        self.assertEqual(batch.status, "blocked")
        with patch.object(service.settings, "STOCK_PUBLICATION_WRITE_ENABLED", True):
            with self.assertRaises(service.PublicationError):
                await self.submit(batch)
        self.write.assert_not_awaited()

    async def test_successful_publication_puts_each_exact_leaf_once_and_keeps_hold_until_release(self):
        before = await self.physical_snapshot()
        batch = await self.prepare(["SKU-A", "SKU-B"])
        with patch.object(service.settings, "STOCK_PUBLICATION_WRITE_ENABLED", True):
            await self.submit(batch)
            await self.submit(batch)
            await self.invoke("process_batch", batch.id)
            await self.invoke("process_batch", batch.id)
        state = await self.state(batch)
        self.assertEqual(state.batch.status, "completed")
        self.assertEqual([item.status for item in state.items], ["verified", "verified"])
        self.assertEqual([item.after_quantity for item in state.items], ["4", "4"])
        self.assertTrue(state.hold.active)
        self.assertEqual(self.write.await_count, 2)
        self.assertEqual({call.args[1]["code"] for call in self.write.await_args_list}, {"SKU-A", "SKU-B"})
        self.assertEqual(await self.physical_snapshot(), before)
        await self.invoke("release_hold", state.hold.id,
                          StockPublicationReleaseHold(confirmed=True, maintenance_completed=True))
        self.assertFalse((await self.state(batch)).hold.active)

    async def test_cancel_before_send_prevents_put_and_allows_explicit_hold_release(self):
        batch = await self.prepare()
        before = await self.physical_snapshot()
        with patch.object(service.settings, "STOCK_PUBLICATION_WRITE_ENABLED", True):
            await self.submit(batch)
            await self.invoke("cancel", batch.id, StockPublicationConfirmed(confirmed=True))
            await self.invoke("process_batch", batch.id)
        state = await self.state(batch)
        self.assertEqual(state.batch.status, "cancelled")
        self.assertTrue(all(item.status == "cancelled" for item in state.items))
        self.assertTrue(state.hold.active)
        self.write.assert_not_awaited()
        await self.invoke("release_hold", state.hold.id,
                          StockPublicationReleaseHold(confirmed=True, maintenance_completed=True))
        self.assertFalse((await self.state(batch)).hold.active)
        self.assertEqual(await self.physical_snapshot(), before)

    async def test_cancel_during_final_read_is_rechecked_before_durable_sending_intent(self):
        batch = await self.prepare()
        async def cancel_during_read(*args):
            observed = await self.read_remote(*args)
            async with self.sessions() as other:
                await service.cancel(other, batch.id, StockPublicationConfirmed(confirmed=True))
            return observed
        with patch.object(service.settings, "STOCK_PUBLICATION_WRITE_ENABLED", True):
            await self.submit(batch)
            self.read.side_effect = cancel_during_read
            await self.invoke("process_batch", batch.id)
        self.assertEqual((await self.state(batch)).batch.status, "cancelled")
        self.write.assert_not_awaited()

    async def test_timed_out_put_is_quarantined_without_retry_or_automatic_hold_expiry(self):
        batch = await self.prepare()
        before = await self.physical_snapshot()
        intent_observations = []
        async def ambiguous_write(*args):
            intent = await self.state(batch)
            intent_observations.append((self.network_session.get().in_transaction(), intent.items[0].status,
                                        bool(intent.items[0].attempt_id), intent.hold.active))
            # The remote request may have applied despite an unconfirmed response.
            self.remote["SKU-A"]["quantity"] = "4"
            raise source.SourceError("stock_publication_write_unconfirmed", uncertain=True)
        self.write.side_effect = ambiguous_write
        with patch.object(service.settings, "STOCK_PUBLICATION_WRITE_ENABLED", True):
            await self.submit(batch)
            await self.invoke("process_batch", batch.id)
            with patch.object(service, "now", return_value=NOW + timedelta(days=30)):
                await self.invoke("recover")
                await self.invoke("process_batch", batch.id)
            with self.assertRaises(service.PublicationError):
                await self.submit(batch)
        state = await self.state(batch)
        self.assertEqual((state.batch.status, state.items[0].status), ("uncertain", "uncertain"))
        await self.invoke("verify", batch.id, StockPublicationConfirmed(confirmed=True))
        self.assertEqual((await self.state(batch)).batch.status, "uncertain",
                         "A matching GET cannot prove the old request has finished")
        with self.assertRaises(service.PublicationError):
            await self.invoke("release_hold", state.hold.id,
                              StockPublicationReleaseHold(confirmed=True, maintenance_completed=True))
        self.assertTrue((await self.state(batch)).hold.active)
        self.assertEqual(intent_observations, [(False, "sending", True, True)])
        self.write.assert_awaited_once()
        self.assertEqual(await self.physical_snapshot(), before)

    async def test_recovery_quarantines_committed_sending_intent_without_resending(self):
        batch = await self.prepare()
        with patch.object(service.settings, "STOCK_PUBLICATION_WRITE_ENABLED", True):
            await self.submit(batch)
        async with self.transaction() as db:
            await db.execute(update(StockPublicationBatch).where(StockPublicationBatch.id == batch.id)
                .values(status="running", started_at=NOW))
            await db.execute(update(StockPublicationItem).where(StockPublicationItem.batch_id == batch.id)
                .values(status="sending", attempt_id=str(uuid4()), attempt_started_at=NOW))
        await self.invoke("recover")
        state = await self.state(batch)
        self.assertEqual((state.batch.status, state.items[0].status), ("uncertain", "uncertain"))
        self.assertTrue(state.hold.active)
        with patch.object(service.settings, "STOCK_PUBLICATION_WRITE_ENABLED", True):
            await self.invoke("process_batch", batch.id)
        self.write.assert_not_awaited()

    async def test_explicit_resolution_requires_matching_readback_before_releasing_quarantine(self):
        batch = await self.prepare()
        self.write.side_effect = source.SourceError("stock_publication_write_unconfirmed", uncertain=True)
        with patch.object(service.settings, "STOCK_PUBLICATION_WRITE_ENABLED", True):
            await self.submit(batch)
            await self.invoke("process_batch", batch.id)
        confirmation = StockPublicationResolve(confirmed=True, external_requests_finished=True)
        # Attestation alone cannot resolve an unreadable remote result.
        self.read.side_effect = source.SourceError("stock_publication_source_unavailable")
        try:
            await self.invoke("resolve", batch.id, confirmation)
        except service.PublicationError:
            pass
        self.assertEqual((await self.state(batch)).items[0].status, "uncertain")
        self.read.side_effect = self.read_remote
        self.remote["SKU-A"]["quantity"] = "4"
        await self.invoke("resolve", batch.id, confirmation)
        state = await self.state(batch)
        self.assertEqual((state.batch.status, state.items[0].status), ("completed", "verified"))
        self.assertTrue(state.items[0].resolution)
        self.assertTrue(state.hold.active)
        self.write.assert_awaited_once()

    async def test_resolution_records_mismatched_readback_as_failed_without_another_put(self):
        batch = await self.prepare()
        self.write.side_effect = source.SourceError("stock_publication_write_unconfirmed", uncertain=True)
        with patch.object(service.settings, "STOCK_PUBLICATION_WRITE_ENABLED", True):
            await self.submit(batch)
            await self.invoke("process_batch", batch.id)
        await self.invoke("resolve", batch.id,
                          StockPublicationResolve(confirmed=True, external_requests_finished=True))
        state = await self.state(batch)
        self.assertEqual((state.batch.status, state.items[0].status), ("blocked", "failed"))
        self.assertEqual(state.items[0].after_quantity, "9")
        self.assertTrue(state.items[0].resolution)
        self.assertTrue(state.hold.active)
        self.write.assert_awaited_once()

    async def assert_local_drift_blocks_submit(self, mutate):
        batch = await self.prepare()
        await mutate(batch)
        before = await self.physical_snapshot()
        with patch.object(service.settings, "STOCK_PUBLICATION_WRITE_ENABLED", True):
            with self.assertRaises(service.PublicationError):
                await self.submit(batch)
        self.write.assert_not_awaited()
        self.assertEqual(await self.physical_snapshot(), before)

    async def test_order_policy_drift_blocks_frozen_draft(self):
        async def mutate(batch):
            async with self.transaction() as db:
                await db.execute(update(OrderStockPolicy).where(OrderStockPolicy.shop_id == batch.shop_id)
                    .values(revision=2))
        await self.assert_local_drift_blocks_submit(mutate)

    async def test_hold_drift_blocks_frozen_draft(self):
        async def mutate(batch):
            async with self.transaction() as db:
                await db.execute(update(StockPublicationHold).where(StockPublicationHold.id == batch.hold_id)
                    .values(active=False, closed_at=NOW))
        await self.assert_local_drift_blocks_submit(mutate)

    async def test_mapping_drift_blocks_frozen_draft(self):
        async def mutate(batch):
            async with self.transaction() as db:
                await db.execute(update(ShopProduct).where(ShopProduct.shop_id == batch.shop_id,
                    ShopProduct.product_id == self.products["SKU-A"]).values(parent_code="OTHER-PARENT"))
        await self.assert_local_drift_blocks_submit(mutate)

    async def test_local_reservation_drift_blocks_frozen_draft(self):
        async def mutate(batch):
            async with self.transaction() as db:
                await db.execute(update(StockBalance).where(StockBalance.product_id == self.products["SKU-A"])
                    .values(qty_reserved=D("4")))
        await self.assert_local_drift_blocks_submit(mutate)

    async def test_fresh_remote_stock_drift_blocks_send_without_put(self):
        batch = await self.prepare()
        with patch.object(service.settings, "STOCK_PUBLICATION_WRITE_ENABLED", True):
            await self.submit(batch)
            self.remote["SKU-A"]["quantity"] = "6"
            await self.invoke("process_batch", batch.id)
        self.assertEqual((await self.state(batch)).batch.status, "blocked")
        self.assertTrue((await self.state(batch)).hold.active)
        self.write.assert_not_awaited()

    async def test_disabled_shop_policy_after_queue_blocks_worker_despite_enabled_server(self):
        batch = await self.prepare()
        with patch.object(service.settings, "STOCK_PUBLICATION_WRITE_ENABLED", True):
            await self.submit(batch)
            await self.configure(enabled=False, revision=1)
            await self.invoke("process_batch", batch.id)
        state = await self.state(batch)
        self.assertEqual((state.batch.status, state.batch.error), ("blocked", "stock_publication_policy_disabled"))
        self.assertTrue(state.hold.active)
        self.write.assert_not_awaited()

    async def test_changed_remote_native_identity_blocks_same_sku_without_put(self):
        batch = await self.prepare()
        with patch.object(service.settings, "STOCK_PUBLICATION_WRITE_ENABLED", True):
            await self.submit(batch)
            self.remote["SKU-A"]["identity"]["variant_id"] = 999
            await self.invoke("process_batch", batch.id)
        state = await self.state(batch)
        self.assertEqual((state.batch.status, state.batch.error), ("blocked", "stock_publication_identity_changed"))
        self.assertTrue(state.hold.active)
        self.write.assert_not_awaited()

    async def test_late_prepared_verification_cannot_overwrite_completed_publication_evidence(self):
        batch = await self.prepare()
        captured, release = asyncio.Event(), asyncio.Event()
        async def delayed_read(*args):
            observation = await self.read_remote(*args)
            if not captured.is_set():
                captured.set()
                await asyncio.wait_for(release.wait(), timeout=5)
            return observation
        self.read.side_effect = delayed_read
        verification = asyncio.create_task(self.invoke("verify", batch.id, StockPublicationConfirmed(confirmed=True)))
        try:
            await asyncio.wait_for(captured.wait(), timeout=5)
            with patch.object(service.settings, "STOCK_PUBLICATION_WRITE_ENABLED", True):
                await self.submit(batch)
                await self.invoke("process_batch", batch.id)
            completed = await self.state(batch)
            self.assertEqual((completed.batch.status, completed.items[0].after_quantity), ("completed", "4"))
            release.set()
            with self.assertRaises(service.PublicationError) as raised:
                await asyncio.wait_for(verification, timeout=5)
            self.assertEqual(raised.exception.code, "stock_publication_observation_changed")
        finally:
            release.set()
            if not verification.done():
                verification.cancel()
                await asyncio.gather(verification, return_exceptions=True)
        final = await self.state(batch)
        self.assertEqual((final.items[0].status, final.items[0].after_quantity), ("verified", "4"))
        self.assertEqual(final.items[0].observation, completed.items[0].observation)
        self.assertEqual(final.items[0].acknowledgement, completed.items[0].acknowledgement)
        self.write.assert_awaited_once()

    async def test_remote_rate_limit_persists_shop_cooldown_and_stops_remaining_reads(self):
        batch = await self.prepare(["SKU-A", "SKU-B"])
        before = await self.physical_snapshot()
        reads_before = self.read.await_count
        self.read.side_effect = source.SourceError("stock_publication_rate_limited", 429, retry_after=900)
        with patch.object(service.settings, "STOCK_PUBLICATION_WRITE_ENABLED", True):
            await self.submit(batch)
            await self.invoke("process_batch", batch.id)
        state = await self.state(batch)
        self.assertEqual((state.batch.status, state.batch.error), ("blocked", "stock_publication_rate_limited"))
        self.assertEqual(self.read.await_count, reads_before + 1)
        async with self.sessions() as db:
            settings_row = await db.get(StockShopSettings, self.shops["biketrek"])
            self.assertEqual(settings_row.processing_retry_after_at, NOW + timedelta(seconds=900))
        await self.invoke("verify", batch.id, StockPublicationConfirmed(confirmed=True))
        self.assertEqual(self.read.await_count, reads_before + 1)
        self.assertEqual((await self.state(batch)).items[0].observation["error"], "stock_publication_retry_later")
        self.write.assert_not_awaited()
        self.assertEqual(await self.physical_snapshot(), before)

    async def test_first_rate_limit_creates_durable_settings_cooldown_and_invalidates_old_authority(self):
        async with self.transaction() as db:
            await db.execute(delete(StockShopSettings).where(StockShopSettings.shop_id == self.shops["biketrek"]))
        await self.configure()
        await self.open_hold()
        self.read.side_effect = source.SourceError("stock_publication_rate_limited", 429, retry_after=900)
        with self.assertRaises(service.PublicationError) as raised:
            await self.preview()
        self.assertEqual(raised.exception.code, "stock_publication_configuration_changed")
        async with self.sessions() as db:
            settings_row = await db.get(StockShopSettings, self.shops["biketrek"])
            self.assertIsNotNone(settings_row)
            self.assertIsNotNone(settings_row.updated_at)
            self.assertEqual((settings_row.mode, settings_row.processing_retry_after_at),
                             ("manual", NOW + timedelta(seconds=900)))
        self.read.assert_awaited_once()
        self.write.assert_not_awaited()

    async def test_warehouse_hold_refuses_shared_balance_writer_before_any_balance_change(self):
        await self.configure()
        await self.open_hold()
        before = await self.physical_snapshot()
        for create_missing in (False, True):
            with self.assertRaises(StockPublicationHoldError) as raised:
                async with self.transaction() as db:
                    await stock_balances.lock_stock_balances(db, {self.products["SKU-A"]}, self.warehouse_id,
                                                            create_missing=create_missing)
            self.assertEqual(raised.exception.code, "stock_publication_warehouse_held")
        self.assertEqual(await self.physical_snapshot(), before)

    async def test_receiving_finalize_rejects_hold_without_finalizing_receipt_or_changing_stock(self):
        async with self.transaction() as db:
            supplier = Supplier(code="publication-receipt", name="Publication receipt")
            db.add(supplier)
            await db.flush()
            receipt = ReceivingSession(supplier_id=supplier.id, warehouse_id=self.warehouse_id,
                invoice_number="PUBLICATION-GATE", status=ReceivingStatus.in_progress, total_lines=1, session_data={})
            db.add(receipt)
            await db.flush()
            receipt_id = receipt.id
            db.add(ReceivingLine(session_id=receipt.id, line_number=1, product_id=self.products["SKU-A"],
                ordered_qty=D("2"), received_qty=D("2"), unit_price=D("3"), status="matched"))
        await self.configure()
        await self.open_hold()
        before = await self.physical_snapshot()
        with patch.object(receiving_db, "_product_code_prefix", return_value="TEST-"), \
             self.assertRaises(HTTPException) as raised:
            async with self.transaction() as db:
                await receiving_db.finalize_session("publication-receipt", receipt_id, receiving_db.FinalizeRequest(), db)
        self.assertEqual((raised.exception.status_code, raised.exception.detail["code"]),
                         (409, "stock_publication_warehouse_held"))
        self.assertEqual(await self.physical_snapshot(), before)
        async with self.sessions() as db:
            self.assertEqual((await db.get(ReceivingSession, receipt_id)).status, ReceivingStatus.in_progress)

    async def test_opening_stock_finalize_rejects_hold_before_creating_first_balance(self):
        async with self.transaction() as db:
            product = Product(sku="UNOPENED", name="No physical history")
            db.add(product)
            await db.flush()
            product_id = product.id
        with patch.object(opening_stock, "now", return_value=NOW):
            async with self.sessions() as db:
                prepared = await opening_stock.preview(db, OpeningPreviewRequest(request_id=uuid4(),
                    warehouse_code="publication-central", source_reference="Synthetic count", operator_name="Test operator",
                    counted_at=NOW, csv_text="sku;quantity;unit_cost;unit\nUNOPENED;2;3;ks\n"))
            self.assertTrue(prepared["ready"])
            batch = prepared["batch"]
            await self.configure()
            await self.open_hold()
            before = await self.physical_snapshot()
            with self.assertRaises(opening_stock.OpeningError) as raised:
                async with self.transaction() as db:
                    await opening_stock.finalize(db, batch["id"], OpeningFinalizeRequest(
                        preview_hash=batch["preview_hash"], confirmed=True, receipts_reconciled=True))
        self.assertEqual(raised.exception.code, "stock_publication_warehouse_held")
        self.assertEqual(await self.physical_snapshot(), before)
        async with self.sessions() as db:
            self.assertIsNone(await db.scalar(select(StockBalance).where(StockBalance.product_id == product_id)))

    async def test_order_reservation_rejects_hold_without_creating_items_or_changing_allocation(self):
        observed = {"uuid": str(uuid4()), "order_number": "PUBLICATION-HOLD-ORDER",
            "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(), "origin": "eshop",
            "status_id": 1, "paid": False, "resolved": False,
            "lines": [{"line_key": str(uuid4()), "code": "SKU-A", "title": "A", "ean": "", "quantity": "1",
                "unit": "ks", "kind": "product", "parent_uuid": "", "length": "", "length_unit": "",
                "has_native_identity": False, "identity_invalid": False, "classification": "identified",
                "product_id": self.products["SKU-A"], "sku": "SKU-A", "matched_by": "shared_sku", "reasons": []}]}
        async with self.transaction() as db:
            shop = await db.get(Shop, self.shops["biketrek"])
            order = await order_stock_ledger.get_or_create_order(db, shop, observed, self.warehouse_id)
            order_id = order.id
            plan = await order_stock_ledger.plan_order(db, order, observed, "reserve", self.warehouse_id)
            self.assertTrue(plan["ready"])
        await self.configure()
        await self.open_hold()
        before = await self.physical_snapshot()
        with self.assertRaises(order_stock_ledger.OrderStockError) as raised:
            async with self.transaction() as db:
                order = await db.get(ShopOrder, order_id)
                await order_stock_ledger.apply_order(db, order, observed, "reserve", self.warehouse_id, plan)
        self.assertEqual(raised.exception.code, "stock_publication_warehouse_held")
        self.assertEqual(await self.physical_snapshot(), before)

    async def test_hold_opener_waits_for_admitted_writer_transaction_then_blocks_next_writer(self):
        await self.configure()
        async with self.sessions() as writer:
            balances, _ = await stock_balances.lock_stock_balances(writer, {self.products["SKU-A"]}, self.warehouse_id)
            opening = asyncio.create_task(self.open_hold())
            try:
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(opening), timeout=0.1)
                balances[self.products["SKU-A"]].qty_reserved = D("2")
                await writer.commit()
                hold = await asyncio.wait_for(opening, timeout=5)
            finally:
                if not opening.done():
                    opening.cancel()
                    await asyncio.gather(opening, return_exceptions=True)
        self.assertTrue(hold.active)
        async with self.sessions() as db:
            balance = await db.scalar(select(StockBalance).where(StockBalance.product_id == self.products["SKU-A"]))
            self.assertEqual(balance.qty_reserved, D("2"))
        with self.assertRaises(StockPublicationHoldError):
            async with self.transaction() as db:
                await stock_balances.lock_stock_balances(db, {self.products["SKU-A"]}, self.warehouse_id)
