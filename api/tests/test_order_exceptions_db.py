"""Exception closure is an audit, never a synthetic physical return."""
import asyncio
import copy
import unittest
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import select, text

import test_order_processing_db as fixture
from test_order_stock import NOW, raw_line
from inventory_hub.fifo_return_types import FifoReturnInput
from inventory_hub.fifo_types import FifoCutoverApply, FifoCutoverPreview
from inventory_hub.order_collection_models import OrderInboxEntry
from inventory_hub.order_exception_types import ResolutionConfirmRequest, ResolutionPreviewRequest
from inventory_hub.order_processing_models import OrderProcessingJob
from inventory_hub.order_stock_models import OrderStockPreview
from inventory_hub.order_stock_types import OrderStockApplyRequest
from inventory_hub.services import fifo, fifo_returns, order_exceptions as service, order_processing, order_stock
from inventory_hub.services import stock_settings


@unittest.skipUnless(fixture.TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class OrderExceptionDatabaseTests(unittest.IsolatedAsyncioTestCase):
    # Reuse fixture setup/helpers, not its tests (discovery must execute each once).
    asyncSetUp = fixture.OrderProcessingDatabaseTests.asyncSetUp
    asyncTearDown = fixture.OrderProcessingDatabaseTests.asyncTearDown
    transaction = fixture.OrderProcessingDatabaseTests.transaction
    client = fixture.OrderProcessingDatabaseTests.client
    raw = fixture.OrderProcessingDatabaseTests.raw
    seed = fixture.OrderProcessingDatabaseTests.seed
    observe = fixture.OrderProcessingDatabaseTests.observe
    start = fixture.OrderProcessingDatabaseTests.start
    fetch = fixture.OrderProcessingDatabaseTests.fetch
    finish = fixture.OrderProcessingDatabaseTests.finish
    process = fixture.OrderProcessingDatabaseTests.process
    state = fixture.OrderProcessingDatabaseTests.state
    physical_snapshot = fixture.OrderProcessingDatabaseTests.physical_snapshot

    async def review(self, raw):
        plan = await self.start(raw)
        try:
            await self.finish(plan)
        except (order_processing.ProcessingError, order_stock.OrderStockError) as error:
            async with self.transaction() as db:
                await order_processing.fail_job(db, plan, error.code)
        else:
            self.fail("Expected issued order to require review")
        return plan["id"]

    async def issued_review(self, status=2, quantity="1", *, seed=True):
        if seed:
            await self.seed("5")
        raw = self.raw(raw_line(quantity=quantity), status_id=8)
        await self.process(raw)
        raw.update(status_id=status, last_update_time=NOW.isoformat())
        job_id = await self.review(raw)
        return job_id, raw

    async def preview(self, job_id, identifier=None):
        async with self.sessions() as db:
            return await service.preview(db, job_id, ResolutionPreviewRequest(request_id=identifier or uuid4()))

    def confirmation(self, preview, **changes):
        return ResolutionConfirmRequest(preview_hash=preview["preview_hash"], confirmed=True,
            stock_accounted=True, reason=changes.get("reason", "Physical issue and ledger checked"))

    async def resolve(self, preview, **changes):
        async with self.sessions() as db:
            return await service.resolve(db, preview["id"], self.confirmation(preview, **changes), "user:17")

    async def test_cancel_after_issue_closes_stock_review_without_changing_original_ledger(self):
        job_id, _ = await self.issued_review()
        before = await self.physical_snapshot()
        preview = await self.preview(job_id)
        self.assertEqual(preview["source_status_name"], "Storno")
        self.assertFalse(preview["lines"][0]["physical_return_supported"])
        self.assertIsNone(preview["lines"][0]["returned_qty"])
        result = await self.resolve(preview)
        self.assertEqual((result["action"], result["movements_created"], result["actor"]), ("resolve_exception", 0, "user:17"))
        self.assertEqual(await self.physical_snapshot(), before)
        self.assertEqual((await self.state()).jobs[0]["status"], "completed")
        self.assertEqual(await self.process(), result, "Same-source background check must retain reviewed closure")
        self.assertEqual(await self.physical_snapshot(), before)

    async def test_preview_and_confirmation_recover_exact_requests_and_reject_changed_confirmation(self):
        job_id, _ = await self.issued_review(status=25)
        identifier = uuid4()
        preview = await self.preview(job_id, identifier)
        self.assertEqual(await self.preview(job_id, identifier), preview)
        with self.assertRaisesRegex(service.ExceptionError, "request_changed"):
            await self.preview(job_id + 1, identifier)
        result = await self.resolve(preview)
        self.assertEqual(await self.resolve(preview), result)
        async with self.sessions() as db:
            self.assertEqual((await service.get_preview(db, identifier))["result"], result)
        with self.assertRaisesRegex(service.ExceptionError, "confirmation_changed"):
            await self.resolve(preview, reason="Different review")
        async with self.sessions() as db:
            self.assertEqual(len((await db.scalars(select(OrderStockPreview).where(
                OrderStockPreview.preview_data["trigger"].astext == "exception_resolution"))).all()), 1)

    async def test_changed_source_without_new_header_cannot_confirm_or_reuse_resolution(self):
        job_id, raw = await self.issued_review(status=25)
        preview = await self.preview(job_id)
        before = await self.physical_snapshot()
        self.raw_orders[raw["order_number"]]["products"][0]["quantity"] = "2"
        with self.assertRaises(service.ExceptionError):
            await self.resolve(preview)
        self.assertEqual(await self.physical_snapshot(), before)
        self.raw_orders[raw["order_number"]] = copy.deepcopy(raw)
        await self.resolve(preview)
        self.raw_orders[raw["order_number"]]["paid_date"] = "2026-09-23"
        plan = await self.start()
        with self.assertRaises(order_stock.OrderStockError):
            await self.finish(plan)

    async def test_metadata_generation_configuration_and_revision_changes_invalidate_preview(self):
        job_id, _ = await self.issued_review()
        preview = await self.preview(job_id)
        mutations = (
            "UPDATE order_processing_jobs SET generation = generation + 1",
            "UPDATE order_inbox SET observation_hash = repeat('a', 64)",
            "UPDATE order_inbox SET deleted = true",
            "UPDATE order_inbox SET review_reason = 'order_collection_identity_conflict'",
            "UPDATE stock_shop_settings SET revision = revision + 1",
            "UPDATE shop_orders SET stock_revision = stock_revision + 1",
        )
        for sql in mutations:
            async with self.sessions() as db:
                await db.execute(text(sql))
                with self.subTest(sql=sql), self.assertRaises((service.ExceptionError, order_processing.ProcessingError)):
                    await service.resolve(db, preview["id"], self.confirmation(preview), "user:17")
                await db.rollback()

    async def test_new_collector_observation_reopens_completed_resolution_and_gate_rejects_stale_audit(self):
        from inventory_hub.services.order_sync_readiness import has_unaccounted_orders
        job_id, raw = await self.issued_review()
        await self.resolve(await self.preview(job_id))
        async with self.sessions() as db:
            job = await db.get(OrderProcessingJob, job_id)
            entry = await db.get(OrderInboxEntry, job.inbox_id)
            config = await stock_settings.effective(db, job.shop_id)
            self.assertIsNotNone(await service.current_resolution(db, job, entry, config, self.warehouse_id))
            self.assertFalse(await has_unaccounted_orders(db, job.shop_id, self.warehouse_id, config))
        raw["last_update_time"] = (NOW + timedelta(seconds=1)).isoformat()
        await self.observe(raw)
        async with self.sessions() as db:
            job = await db.get(OrderProcessingJob, job_id)
            entry = await db.get(OrderInboxEntry, job.inbox_id)
            config = await stock_settings.effective(db, job.shop_id)
            self.assertIsNone(await service.current_resolution(db, job, entry, config, self.warehouse_id))
            self.assertTrue(await has_unaccounted_orders(db, job.shop_id, self.warehouse_id, config))
        await self.review(raw)
        self.assertEqual((await self.state()).jobs[0]["status"], "review")

    async def test_reserved_order_has_no_resolution_button_or_issued_review_bypass(self):
        await self.seed()
        raw = self.raw()
        await self.process(raw)
        raw.update(status_id=25, last_update_time=NOW.isoformat())
        job_id = await self.review(raw)
        with self.assertRaises(service.ExceptionError):
            await self.preview(job_id)
        async with self.sessions() as db:
            self.assertFalse((await order_processing.jobs(db, "biketrek"))["jobs"][0]["resolution_available"])

    async def test_regular_order_preview_routes_cannot_apply_or_render_exception_audit(self):
        job_id, _ = await self.issued_review()
        preview = await self.preview(job_id)
        async with self.sessions() as db:
            self.assertNotIn(preview["id"], [p["id"] for p in (await order_stock.list_previews(db, "biketrek"))["previews"]])
            with self.assertRaises(order_stock.OrderStockError):
                await order_stock.get_preview(db, preview["id"])
            with self.assertRaises(order_stock.OrderStockError):
                await order_stock.apply(db, preview["id"], OrderStockApplyRequest(preview_hash=preview["preview_hash"],
                    confirmed=True, physical_confirmed=True))

    async def test_removed_historical_line_does_not_block_a_valid_issued_order_review(self):
        await self.seed()
        line_a, line_b = raw_line(), raw_line()
        raw = self.raw(line_a, line_b)
        await self.process(raw)
        raw["products"] = [line_a]
        await self.process(raw)
        raw.update(status_id=8)
        await self.process(raw)
        raw.update(status_id=2, last_update_time=NOW.isoformat())
        job_id = await self.review(raw)
        preview = await self.preview(job_id)
        self.assertEqual(len(preview["lines"]), 1)
        await self.resolve(preview)

    async def test_manual_only_issued_order_can_be_reviewed_without_any_stock_evidence_fabrication(self):
        raw = self.raw(raw_line(code="", title="Service only"), status_id=8)
        await self.process(raw)
        raw.update(status_id=2, last_update_time=NOW.isoformat())
        job_id = await self.review(raw)
        preview = await self.preview(job_id)
        self.assertEqual(preview["lines"], [])
        await self.resolve(preview)
        self.assertEqual((await self.state()).movements, [])

    async def test_concurrent_same_confirmation_creates_only_one_audit_result(self):
        job_id, _ = await self.issued_review()
        preview = await self.preview(job_id)
        before = await self.physical_snapshot()
        left, right = await asyncio.wait_for(asyncio.gather(self.resolve(preview), self.resolve(preview)), 10)
        self.assertEqual(left, right)
        self.assertEqual(await self.physical_snapshot(), before)

    async def test_collector_change_during_source_fetch_and_running_processor_block_confirmation(self):
        job_id, _ = await self.issued_review()
        preview = await self.preview(job_id)
        before = await self.physical_snapshot()
        original_load = service.load_source

        async def changed_during_fetch(*args, **kwargs):
            fetched = await original_load(*args, **kwargs)
            async with self.transaction() as other:
                await other.execute(text("UPDATE order_inbox SET review_reason = 'order_collection_source_conflict'"))
            return fetched

        with patch.object(service, "load_source", side_effect=changed_during_fetch):
            with self.assertRaisesRegex(service.ExceptionError, "context_changed"):
                await self.resolve(preview)
        async with self.transaction() as db:
            await db.execute(text("UPDATE order_inbox SET review_reason = NULL"))
        plan = await self.start()
        self.assertIsNotNone(plan)
        with self.assertRaisesRegex(service.ExceptionError, "context_changed"):
            await self.resolve(preview)
        self.assertEqual(await self.physical_snapshot(), before)

    async def test_partial_fifo_return_is_separate_and_review_never_adds_more_stock(self):
        await self.seed("5")
        async with self.sessions() as db:
            cutover = await fifo.cutover_preview(db, FifoCutoverPreview(request_id=uuid4(), sku="SKU-A",
                warehouse_code="processing-central", source_reference="Verified historical count",
                operator_name="Test", counted_at=NOW, layers=[{"quantity": "5", "unit_cost": "3",
                    "cost_status": "known", "physical_received_at": NOW, "source_reference": "Test receipt"}]))
        async with self.sessions() as db:
            await fifo.cutover_apply(db, cutover["id"], FifoCutoverApply(preview_hash=cutover["preview_hash"],
                confirmed=True, quantities_verified=True, costs_documented=True))
        job_id, _ = await self.issued_review(status=25, quantity="3", seed=False)
        preview = await self.preview(job_id)
        issue_id = preview["lines"][0]["issue_movement_id"]
        async with self.sessions() as db:
            await fifo_returns.receive_return(db, FifoReturnInput(request_id=uuid4(), issue_movement_id=issue_id,
                quantity="1", case_reference="Test return", reason="Received one piece", condition="good",
                confirmed=True, physical_received=True))
        with self.assertRaisesRegex(service.ExceptionError, "ledger_changed"):
            await self.resolve(preview)
        fresh = await self.preview(job_id)
        self.assertEqual(Decimal(fresh["lines"][0]["returned_qty"]), Decimal(1))
        self.assertEqual(Decimal(fresh["lines"][0]["returnable_qty"]), Decimal(2))
        before = await self.physical_snapshot()
        await self.resolve(fresh)
        self.assertEqual(await self.physical_snapshot(), before)
