"""Automatic stock authority and semantic no-op decisions; no live transport."""
import copy
import asyncio
import unittest
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from inventory_hub.services import order_processing as service
from inventory_hub.services import order_stock as stock
from inventory_hub.services import order_stock_source as source
from inventory_hub.services import order_processing_worker as worker
from test_order_stock import ACTIONS, NOW, STATUSES, raw_order, raw_page


AUTOMATION_START = NOW - timedelta(days=2)
ISSUE_START = NOW - timedelta(days=1)


def observation(**changes):
    raw = raw_order(creation_time=(ISSUE_START + timedelta(hours=1)).isoformat(), **changes)
    order, digest = source._order(raw_page(raw), raw["order_number"])
    order["lines"][0].update(classification="identified", product_id=10, sku="SKU-A",
                             matched_by="shared_sku", reasons=[])
    return {"order": order, "source_hash": digest, **source._statuses(STATUSES)}


def configuration(**changes):
    return {"mode": "fulfill", "processing_paused": False,
            "automation_starts_at": AUTOMATION_START, "issue_starts_at": ISSUE_START,
            "authorized_policy_revision": 3, "target_fingerprint": "a" * 64, "warehouse_id": 5, **changes}


def policy(**changes):
    return SimpleNamespace(revision=3, starts_at=NOW - timedelta(days=30), warehouse_id=5,
                           status_hash=source._statuses(STATUSES)["status_hash"],
                           status_actions=dict(ACTIONS), **changes)


class ProcessingAuthorizationTests(unittest.TestCase):
    def setUp(self):
        clock = patch.object(stock, "now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def authorize(self, fetched=None, config=None, configured=None):
        fetched = fetched or observation()
        return service.authorize(config or configuration(), configured or policy(), fetched["order"], fetched)

    def test_manual_pause_and_policy_change_do_not_grant_automatic_stock_authority(self):
        for changes in ({"mode": "manual"}, {"processing_paused": True},
                        {"authorized_policy_revision": 2}, {"automation_starts_at": None}):
            with self.subTest(changes=changes), self.assertRaises(service.ProcessingError):
                self.authorize(config=configuration(**changes))

    def test_reservation_mode_allows_holds_and_cancel_but_never_shipment_or_completed_pos_issue(self):
        config = configuration(mode="reserve", issue_starts_at=None)
        for status_id, action in ((1, "reserve"), (2, "cancel")):
            self.assertEqual(self.authorize(observation(status_id=status_id), config), action)
        for fetched in (observation(status_id=8), observation(status_id=19),
                        observation(origin="cash-register", paid_date="2026-09-23", resolved_yn=True)):
            with self.subTest(source=fetched["order"]), self.assertRaises(service.ProcessingError):
                self.authorize(fetched, config)

    def test_paid_web_order_remains_reserved_and_unpaid_or_incomplete_pos_does_not_issue(self):
        for changes in ({"origin": "eshop", "paid_date": "2026-09-23", "resolved_yn": True},
                        {"origin": "cash-register", "paid_date": None, "resolved_yn": True},
                        {"origin": "cash-register", "paid_date": "2026-09-23", "resolved_yn": False}):
            self.assertEqual(self.authorize(observation(**changes)), "reserve")
        self.assertEqual(self.authorize(observation(origin="cash-register", paid_date="2026-09-23",
                                                   resolved_yn=True)), "issue")

    def test_each_authority_cutover_uses_creation_date_instead_of_last_update(self):
        fetched = observation()
        for boundary, status_id in ((AUTOMATION_START, 1), (ISSUE_START, 8)):
            for offset, allowed in ((timedelta(microseconds=-1), False), (timedelta(0), True)):
                current = copy.deepcopy(fetched)
                current["order"].update(created_at=(boundary + offset).isoformat(), status_id=status_id,
                                         updated_at=NOW.isoformat())
                if allowed:
                    self.assertEqual(self.authorize(current), "reserve" if status_id == 1 else "issue")
                else:
                    with self.subTest(boundary=boundary), self.assertRaises(service.ProcessingError):
                        self.authorize(current)

    def test_fulfillment_requires_its_own_cutover_and_status_snapshot(self):
        with self.assertRaises(service.ProcessingError):
            self.authorize(observation(status_id=8), configuration(issue_starts_at=None))
        fetched = observation(status_id=8)
        fetched["status_hash"] = "b" * 64
        with self.assertRaises((service.ProcessingError, stock.OrderStockError)):
            self.authorize(fetched)
        with self.assertRaises((service.ProcessingError, stock.OrderStockError)):
            self.authorize(observation(status_id=25))


class ProcessingNoopTests(unittest.TestCase):
    def setUp(self):
        self.fetched = observation()
        self.source = self.fetched["order"]
        self.order = SimpleNamespace(stock_state="reserved", stock_source_hash=self.fetched["source_hash"],
                                     stock_snapshot=copy.deepcopy(self.source))
        self.plan = {"ready": True, "errors": [], "effects": [{"product_id": 10,
                     "old_allocation": "1", "allocation": "1", "shortage": "0"}]}

    def unchanged(self, action="reserve"):
        return service.unchanged_hold(self.order, self.fetched["source_hash"], self.source, action, self.plan)

    def test_identical_reservation_and_unchanged_backorder_do_not_churn_ledger_revisions(self):
        self.assertTrue(self.unchanged())
        self.plan["effects"][0].update(old_allocation="1.000", allocation="1")
        self.assertTrue(self.unchanged(), "Equivalent stored decimal scales must not trigger an edit")
        self.plan["effects"][0].update(old_allocation="0", allocation="0", shortage="1")
        self.assertTrue(self.unchanged(), "Waiting for a receipt must not repeatedly rewrite identical reservations")

    def test_newly_available_backorder_and_reduced_allocation_are_real_changes_even_without_source_change(self):
        for previous, current in (("0", "1"), ("1", "0")):
            self.plan["effects"][0].update(old_allocation=previous, allocation=current)
            self.assertFalse(self.unchanged(), "A source-only hash cannot decide whether stock work is complete")

    def test_unchanged_source_does_not_hide_re_resolved_identity_or_blocked_plan(self):
        self.source["lines"][0]["product_id"] = 99
        self.assertFalse(self.unchanged())
        self.source["lines"][0]["product_id"] = 10
        self.plan["ready"] = False
        self.assertFalse(self.unchanged())

    def test_only_already_applied_matching_states_are_noops(self):
        for state, action in (("pending", "reserve"), ("cancelled", "reserve"),
                              ("reserved", "cancel"), ("issued", "issue")):
            self.order.stock_state = state
            self.assertFalse(self.unchanged(action))
        self.order.stock_state = "cancelled"
        self.plan["effects"] = []
        self.assertTrue(self.unchanged("cancel"))
        self.order.stock_source_hash = "c" * 64
        self.assertFalse(self.unchanged("cancel"))


class ProcessingWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events, self.sessions = [], []
        self.plan = {"id": 1, "shop_id": 2, "shop_code": "biketrek", "order_number": "TEST-01",
                     "target_fingerprint": "d" * 64, "values": {"run_timeout_seconds": 30}}

    @asynccontextmanager
    async def transaction(self):
        db = SimpleNamespace(number=len(self.sessions) + 1)
        self.sessions.append(db)
        self.events.append((db.number, "begin"))
        try:
            yield db
        except BaseException:
            self.events.append((db.number, "rollback"))
            raise
        else:
            self.events.append((db.number, "commit"))

    async def test_fetch_is_bound_to_authorized_target_and_finishes_in_a_new_transaction(self):
        fetched, result = observation(), {"stock_state": "reserved"}

        async def finish(db, plan, source_data):
            self.assertEqual(self.events, [(1, "begin"), (1, "commit"), (2, "begin")])
            self.assertIs(db, self.sessions[1])
            self.assertIs(plan, self.plan)
            self.assertIs(source_data, fetched)
            return result

        with patch.object(worker, "get_session_context", self.transaction), \
             patch.object(worker, "load_source", AsyncMock(return_value=fetched)) as fetch, \
             patch.object(service, "finish_job", AsyncMock(side_effect=finish)), \
             patch.object(service, "fail_job", AsyncMock()) as fail:
            self.assertEqual(await worker.process(self.plan), result)
        self.assertIs(fetch.await_args.args[0], self.sessions[0])
        self.assertEqual(fetch.await_args.args[1].code, "biketrek")
        self.assertEqual(fetch.await_args.args[2], "TEST-01")
        self.assertEqual(fetch.await_args.kwargs, {"expected_target_fingerprint": "d" * 64})
        self.assertEqual(self.events[-1], (2, "commit"))
        fail.assert_not_awaited()

    async def test_source_failure_rolls_back_then_records_safe_failure_without_entering_ledger(self):
        with patch.object(worker, "get_session_context", self.transaction), \
             patch.object(worker, "load_source", AsyncMock(side_effect=source.SourceError("order_stock_source_unavailable"))), \
             patch.object(service, "finish_job", AsyncMock()) as finish, \
             patch.object(service, "fail_job", AsyncMock()) as fail:
            self.assertIsNone(await worker.process(self.plan))
        finish.assert_not_awaited()
        self.assertEqual(self.events, [(1, "begin"), (1, "rollback"), (2, "begin"), (2, "commit")])
        self.assertEqual(fail.await_args.args[:3], (self.sessions[1], self.plan, "order_stock_source_unavailable"))

    async def test_final_transaction_failure_rolls_back_before_job_error_is_recorded(self):
        with patch.object(worker, "get_session_context", self.transaction), \
             patch.object(worker, "load_source", AsyncMock(return_value=observation())), \
             patch.object(service, "finish_job", AsyncMock(side_effect=service.ProcessingError("order_processing_paused"))), \
             patch.object(service, "fail_job", AsyncMock()) as fail:
            self.assertIsNone(await worker.process(self.plan))
        self.assertEqual(self.events, [(1, "begin"), (1, "commit"), (2, "begin"), (2, "rollback"),
                                       (3, "begin"), (3, "commit")])
        self.assertEqual(fail.await_args.args[:3], (self.sessions[2], self.plan, "order_processing_paused"))

    async def test_cancellation_preserves_durable_running_attempt_for_recovery(self):
        with patch.object(worker, "get_session_context", self.transaction), \
             patch.object(worker, "load_source", AsyncMock(side_effect=asyncio.CancelledError())), \
             patch.object(service, "finish_job", AsyncMock()) as finish, \
             patch.object(service, "fail_job", AsyncMock()) as fail:
            with self.assertRaises(asyncio.CancelledError):
                await worker.process(self.plan)
        finish.assert_not_awaited()
        fail.assert_not_awaited()
        self.assertEqual(self.events, [(1, "begin"), (1, "rollback")])

    async def test_rate_limit_delay_survives_source_error_boundary(self):
        error = source.SourceError("order_stock_rate_limited", 429, retry_after=900)
        with patch.object(worker, "get_session_context", self.transaction), \
             patch.object(worker, "load_source", AsyncMock(side_effect=error)), \
             patch.object(service, "finish_job", AsyncMock()) as finish, \
             patch.object(service, "fail_job", AsyncMock()) as fail:
            self.assertIsNone(await worker.process(self.plan))
        finish.assert_not_awaited()
        fail.assert_awaited_once_with(self.sessions[1], self.plan, "order_stock_rate_limited", 900)
