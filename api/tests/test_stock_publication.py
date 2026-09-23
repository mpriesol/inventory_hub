"""Publication protocol tests use synthetic transport and no shop credentials."""
import unittest
from contextlib import ExitStack
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from pydantic import ValidationError

from inventory_hub.services import stock_publication as service
from inventory_hub.stock_publication_types import (StockPublicationOpenHold, StockPublicationPreview,
    StockPublicationResolve, StockPublicationConfigure)


class PublicationInputTests(unittest.TestCase):
    def test_operator_assertions_require_literal_booleans_and_all_facts(self):
        valid = dict(shop_code="biketrek", confirmed=True, external_writers_paused=True, orders_reconciled=True)
        self.assertTrue(StockPublicationOpenHold(**valid).orders_reconciled)
        for field in ("confirmed", "external_writers_paused", "orders_reconciled"):
            for false_fact in (False, 1, "true", None):
                with self.subTest(field=field, value=false_fact), self.assertRaises(ValidationError):
                    StockPublicationOpenHold(**{**valid, field: false_fact})
        with self.assertRaises(ValidationError):
            StockPublicationResolve(confirmed=True)
        with self.assertRaises(ValidationError):
            StockPublicationConfigure(shop_code="biketrek", confirmed=True, enabled="true", expected_revision=0)

    def test_preview_rejects_duplicate_or_normalized_identity_and_unbounded_batches(self):
        for values in (["SKU", "SKU"], [" SKU"], ["SKU\n"], [""], [str(i) for i in range(101)]):
            with self.subTest(values=values[:2]), self.assertRaises(ValidationError):
                StockPublicationPreview(request_id=uuid4(), shop_code="biketrek", skus=values)
        self.assertEqual(StockPublicationPreview(request_id=uuid4(), shop_code="xtrek", skus=["SKU", "sku"]).skus,
                         ["SKU", "sku"])

    def test_projection_hash_ignores_capture_time_but_keeps_identity_and_quantity(self):
        projection = {"shop_code": "biketrek", "warehouse": {"id": 1}, "captured_at": "old",
                      "rows": [{"sku": "SKU", "target": {"parent_code": "umbrella"}, "qty_available": "4"}]}
        changed = deepcopy(projection)
        changed["captured_at"] = "new"
        self.assertEqual(service._local_hash(projection), service._local_hash(changed))
        changed["rows"][0]["qty_available"] = "3"
        self.assertNotEqual(service._local_hash(projection), service._local_hash(changed))
        changed = deepcopy(projection)
        changed["rows"][0]["target"]["parent_code"] = "genuine-family"
        self.assertNotEqual(service._local_hash(projection), service._local_hash(changed))


class PublicationProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.target = {"code": "SKU", "parent_code": "xTrek", "variant_code": "SKU", "product_id": 20, "variant_id": 21}
        self.batch = SimpleNamespace(id=str(uuid4()), status="queued", expires_at=service.now() + timedelta(minutes=10),
            preview_data={"target_fingerprint": "a" * 64}, error=None)
        self.item = SimpleNamespace(id=1, batch_id=self.batch.id, target=self.target, quantity="4", before_quantity="7",
            status="prepared", attempt_id=None, observation=None, after_quantity=None, error=None,
            attempt_started_at=None, attempt_completed_at=None, verified_at=None, acknowledgement=None)
        self.scope = {"shop": SimpleNamespace(code="biketrek")}
        self.db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock(), flush=AsyncMock())
        self.calls = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        async def scope(*args, **kwargs):
            return self.scope, self.batch
        async def item(*args, **kwargs):
            return self.item
        async def items(*args, **kwargs):
            return [self.item]
        async def dto(*args, **kwargs):
            return {"status": self.batch.status, "item_status": self.item.status, "error": self.batch.error}
        async def maintenance(*args, **kwargs):
            return None, self.batch
        async def stop(db, batch, status, error):
            batch.status, batch.error = status, error
        async def failure(db, identifier, item_id, error, *, uncertain=False, attempt_id=None):
            self.item.status = "uncertain" if uncertain else "failed"
            self.batch.status = "uncertain" if uncertain else "blocked"
            self.batch.error = error
        replacements = {"_batch_scope": scope, "_maintenance_batch": maintenance, "_one": item, "_items": items,
            "_dto": dto, "_stop": stop, "_failure": failure, "_ready": AsyncMock(),
            "_network_ready": AsyncMock(), "_remember_rate_limit": AsyncMock()}
        for name, value in replacements.items():
            self.stack.enter_context(patch.object(service, name, value))
        self.remote = "7"
        async def read(*args):
            self.calls.append("GET")
            return {"identity": deepcopy(self.target), "quantity": self.remote}
        async def write(*args):
            self.calls.append("PUT")
            # Durable intent must already exist before the synthetic request.
            self.assertEqual(self.item.status, "sending")
            self.assertIsNotNone(self.item.attempt_id)
            self.assertGreaterEqual(self.db.commit.await_count, 2)
            self.remote = args[2]
            return {"acknowledged": True}
        self.read = self.stack.enter_context(patch.object(service.source, "read_stock", AsyncMock(side_effect=read)))
        self.write = self.stack.enter_context(patch.object(service.source, "write_stock_once", AsyncMock(side_effect=write)))

    async def test_acknowledged_put_requires_separate_matching_get_and_never_repeats(self):
        result = await service.process_batch(self.db, self.batch.id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.calls, ["GET", "PUT", "GET"])
        self.assertEqual(self.item.after_quantity, "4")
        await service.process_batch(self.db, self.batch.id)
        self.assertEqual(self.calls, ["GET", "PUT", "GET"])

    async def test_already_desired_observation_needs_no_put(self):
        self.remote = "4"
        result = await service.process_batch(self.db, self.batch.id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.calls, ["GET"])
        self.assertIsNone(self.item.attempt_id)

    async def test_unexpected_remote_change_never_creates_send_attempt(self):
        self.remote = "3"
        result = await service.process_batch(self.db, self.batch.id)
        self.assertEqual(result["error"], "stock_publication_remote_changed")
        self.assertIsNone(self.item.attempt_id)
        self.write.assert_not_awaited()

    async def test_lost_acknowledgement_quarantines_without_automatic_get_or_retry(self):
        self.write.side_effect = service.source.SourceError("stock_publication_write_unconfirmed", uncertain=True)
        result = await service.process_batch(self.db, self.batch.id)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(self.read.await_count, 1)
        self.assertEqual(self.write.await_count, 1)
        await service.process_batch(self.db, self.batch.id)
        self.assertEqual(self.write.await_count, 1)

    async def test_definite_put_rejection_is_failed_but_read_failure_after_ack_is_uncertain(self):
        self.write.side_effect = service.source.SourceError("stock_publication_upgates_access", uncertain=False)
        result = await service.process_batch(self.db, self.batch.id)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.item.status, "failed")

    async def test_failed_readback_after_ack_is_uncertain_even_for_definite_read_rejection(self):
        self.read.side_effect = [{"identity": self.target, "quantity": "7"},
            service.source.SourceError("stock_publication_upgates_access", uncertain=False)]
        result = await service.process_batch(self.db, self.batch.id)
        self.assertEqual(result["status"], "uncertain")
        self.write.assert_awaited_once()

    async def test_matching_ack_with_wrong_readback_is_not_verified(self):
        self.read.side_effect = [{"identity": self.target, "quantity": "7"}, {"identity": self.target, "quantity": "6"}]
        result = await service.process_batch(self.db, self.batch.id)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["error"], "stock_publication_readback_mismatch")

    async def test_post_send_local_change_quarantines_matching_remote_readback(self):
        service._ready.side_effect = [None, None, service.PublicationError("stock_publication_local_changed")]
        result = await service.process_batch(self.db, self.batch.id)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(self.calls, ["GET", "PUT", "GET"])
        self.assertEqual(result["error"], "stock_publication_local_changed")


if __name__ == "__main__":
    unittest.main()
