"""AI/manual publication claims share a durable target fence in isolated PostgreSQL."""
import asyncio
from pathlib import Path
import unittest
from uuid import uuid4

from sqlalchemy import select

import test_catalog_db as fixture
from inventory_hub.ai_content_models import AiBatch, AiJob
from inventory_hub.db_models import Product, Shop, Warehouse
from inventory_hub.product_editor_models import ProductEditorPublication
from inventory_hub.services.catalog import CatalogError
from inventory_hub.services.merchandising_write_guard import require_target_available, require_availability_authority_available
from inventory_hub.stock_sync_models import StockSyncItem, StockSyncRun, StockSyncSettings


@unittest.skipUnless(fixture.TEST_URL, "Dedicated localhost *_catalog_test database required")
class MerchandisingWriteGuardDatabaseTests(unittest.IsolatedAsyncioTestCase):
    write_feed = fixture.CatalogDatabaseTests.write_feed
    asyncTearDown = fixture.CatalogDatabaseTests.asyncTearDown

    async def asyncSetUp(self):
        await fixture.CatalogDatabaseTests.asyncSetUp(self)
        async with self.engine.begin() as connection:
            raw = await connection.get_raw_connection()
            for filename in ("005_ai_content.sql", "015_stock_sync.sql", "016_product_publication.sql"):
                await raw.driver_connection.execute((Path(__file__).resolve().parents[2] / "infra/db-init" / filename).read_text())
        async with self.sessions() as db:
            self.shop_id = await db.scalar(select(Shop.id).where(Shop.code == "biketrek"))
            product = Product(sku="GUARD-PART", name="Synthetic guard product")
            batch = AiBatch(id=uuid4().hex, request_hash="a" * 64)
            warehouse = Warehouse(code="guard-central", name="Synthetic guard warehouse")
            db.add_all([product, batch, warehouse]); await db.flush()
            self.product_id, self.batch_id = product.id, batch.id
            self.warehouse_id = warehouse.id
            await db.commit()

    def publication(self, state="sending", parent="POS-xTrek"):
        return ProductEditorPublication(id=str(uuid4()), product_id=self.product_id, shop_id=self.shop_id,
            state=state, document={"identity": {"parent_code": parent, "variant_code": "GUARD-PART"}})

    def ai_job(self, state="sending", parent="POS-xTrek", fields=None):
        return AiJob(id=uuid4().hex, batch_id=self.batch_id, kind="product", status="review",
            context={"shop": "biketrek", "code": parent, "update_preview": {"id": uuid4().hex, "state": state, "fields": fields or ["title"]}},
            checks={}, events=[], usage={}, reserved_usd=0)

    async def insert(self, row):
        async with self.sessions() as db:
            db.add(row); await db.commit()

    async def check(self, shop="biketrek", parent="POS-xTrek", **exclusions):
        async with self.sessions() as db:
            await require_target_available(db, shop, parent, **exclusions)
            await db.commit()

    async def test_manual_sending_or_uncertain_blocks_ai_and_sibling_targets(self):
        row = self.publication(); await self.insert(row)
        for state in ("sending", "uncertain"):
            async with self.sessions() as db:
                current = await db.get(ProductEditorPublication, row.id); current.state = state; await db.commit()
            with self.assertRaises(CatalogError) as error:
                await self.check(parent="pos-XTREK", ai_job_id="a-different-job")
            self.assertEqual(error.exception.code, "merchandising_target_inflight")
            # Unrelated shop or parent remains independent; the owner may recheck itself.
            await self.check(shop="xtrek")
            await self.check(parent="OTHER-PARENT")
            await self.check(publication_id=row.id)

    async def test_ai_sending_or_uncertain_blocks_manual_even_when_job_status_is_review(self):
        row = self.ai_job(); await self.insert(row)
        for state in ("sending", "uncertain"):
            async with self.sessions() as db:
                current = await db.get(AiJob, row.id)
                current.context = {**current.context, "update_preview": {"state": state}}
                await db.commit()
            with self.assertRaises(CatalogError):
                await self.check(publication_id=str(uuid4()))
            await self.check(ai_job_id=row.id)

    async def test_ready_and_terminal_rows_do_not_claim_a_target(self):
        publication = self.publication("ready"); job = self.ai_job("ready")
        await self.insert(publication); await self.insert(job); await self.check()
        for publication_state, ai_state in (("completed", "completed"), ("rejected", "rejected"), ("resolved", "completed")):
            async with self.sessions() as db:
                row = await db.get(ProductEditorPublication, publication.id); row.state = publication_state
                ai = await db.get(AiJob, job.id); ai.context = {**ai.context, "update_preview": {"state": ai_state}}
                await db.commit()
            await self.check()

    async def test_authorized_even_disabled_stock_owner_blocks_only_ai_availability(self):
        policy = StockSyncSettings(shop_id=self.shop_id, warehouse_id=self.warehouse_id, authorized=True, enabled=False)
        await self.insert(policy)
        with self.assertRaises(CatalogError) as error:
            await self.check(availability=True)
        self.assertEqual(error.exception.code, "ai_availability_managed_by_stock")
        await self.check(availability=False)
        await self.check(shop="xtrek", availability=True)
        async with self.sessions() as db:
            row = await db.get(StockSyncSettings, self.shop_id); row.authorized = False; await db.commit()
        await self.check(availability=True)

    async def test_stock_authority_refuses_unresolved_ai_availability_but_allows_text(self):
        job = self.ai_job(fields=["title", "availability"]); await self.insert(job)
        async with self.sessions() as db:
            with self.assertRaises(CatalogError) as error:
                await require_availability_authority_available(db, "biketrek")
            self.assertEqual(error.exception.code, "stock_sync_ai_availability_inflight")
        async with self.sessions() as db:
            row = await db.get(AiJob, job.id)
            row.context = {**row.context, "update_preview": {"state": "uncertain", "fields": ["title"]}}
            await db.commit()
        async with self.sessions() as db:
            await require_availability_authority_available(db, "biketrek")

    async def test_revoked_authority_does_not_release_an_unresolved_stock_write(self):
        await self.insert(StockSyncSettings(shop_id=self.shop_id, warehouse_id=self.warehouse_id, authorized=False, enabled=False))
        run = StockSyncRun(id=str(uuid4()), shop_id=self.shop_id, warehouse_id=self.warehouse_id,
            target_fingerprint="a" * 64, settings_revision=1, trigger="manual", status="uncertain")
        await self.insert(run)
        item = StockSyncItem(run_id=run.id, shop_id=self.shop_id, warehouse_id=self.warehouse_id,
            sku="GUARD-PART", status="sending")
        await self.insert(item)
        for state in ("sending", "uncertain"):
            async with self.sessions() as db:
                row = await db.get(StockSyncItem, item.id); row.status = state; await db.commit()
            with self.assertRaises(CatalogError) as error:
                await self.check(availability=True)
            self.assertEqual(error.exception.code, "ai_availability_managed_by_stock")
            await self.check(availability=False)
        async with self.sessions() as db:
            row = await db.get(StockSyncItem, item.id); row.status = "verified"; await db.commit()
        await self.check(availability=True)

    async def test_authority_claim_serializes_with_new_ai_availability_intent(self):
        started = asyncio.Event()
        async with self.sessions() as first:
            await require_target_available(first, "biketrek", "POS-xTrek", availability=True)
            async def competitor():
                async with self.sessions() as second:
                    started.set()
                    await require_availability_authority_available(second, "biketrek")
            task = asyncio.create_task(competitor())
            try:
                await asyncio.wait_for(started.wait(), 2)
                first.add(self.ai_job(fields=["availability"]))
                await first.commit()
                with self.assertRaises(CatalogError):
                    await asyncio.wait_for(task, 3)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_concurrent_claim_waits_then_observes_committed_intent(self):
        started = asyncio.Event()
        async with self.sessions() as first:
            await require_target_available(first, "biketrek", "POS-xTrek")
            async def competitor():
                async with self.sessions() as second:
                    started.set()
                    await require_target_available(second, "biketrek", "pos-xtrek", publication_id=str(uuid4()))
            task = asyncio.create_task(competitor())
            try:
                await asyncio.wait_for(started.wait(), 2)
                # The first transaction owns the shared claim until its durable intent commits.
                first.add(self.ai_job())
                await first.commit()
                with self.assertRaises(CatalogError):
                    await asyncio.wait_for(task, 3)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
