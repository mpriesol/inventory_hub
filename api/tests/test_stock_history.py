"""History authentication, bounded input and immutable movement presentation."""
from contextlib import ExitStack
from datetime import date, datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.dialects import postgresql

from inventory_hub.database import get_session
from inventory_hub.db_models import MovementType
from inventory_hub.routers import stock_history as routes
from inventory_hub.services import stock_history as service
from inventory_hub.services.upgates import UpgatesClient
from inventory_hub.settings import settings


class StockHistoryRoutesTests(TestCase):
    def setUp(self):
        self.db = SimpleNamespace(commit=AsyncMock(), flush=AsyncMock())
        self.sessions = []

        async def session():
            self.sessions.append(True)
            yield self.db

        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[get_session] = session
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        token = "history-test-only-operator-token-" + "x" * 24
        self.headers = {"Authorization": "Bearer " + token}
        token_patch = patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr(token))
        token_patch.start()
        self.addCleanup(token_patch.stop)

    def test_all_reads_authenticate_before_database_and_never_call_upgates(self):
        with ExitStack() as stack:
            listing = stack.enter_context(patch.object(service, "list_movements", AsyncMock()))
            options = stack.enter_context(patch.object(service, "options", AsyncMock()))
            upstream = stack.enter_context(patch.object(UpgatesClient, "from_shop"))
            for path in ("/stock-history/options", "/stock-history/movements"):
                for headers in ({}, {"Authorization": "Bearer invalid"}):
                    response = self.client.get(path, headers=headers)
                    self.assertEqual(response.status_code, 401, response.text)
                    self.assertEqual(response.headers.get("cache-control"), "no-store")
            self.assertEqual(self.sessions, [])
            listing.assert_not_awaited()
            options.assert_not_awaited()
            upstream.assert_not_called()

    def test_unconfigured_access_fails_closed(self):
        with patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr("")):
            response = self.client.get("/stock-history/movements", headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertEqual(self.sessions, [])

    def test_authorized_read_preserves_exact_sku_and_snapshot_without_writing(self):
        expected = {"items": [], "source": "hub", "upgates_calls": 0, "snapshot_id": 87}
        with patch.object(service, "list_movements", AsyncMock(return_value=expected)) as listing, \
                patch.object(UpgatesClient, "from_shop") as upstream:
            response = self.client.get("/stock-history/movements", headers=self.headers, params={
                "sku": "Case-SKU-01", "q": "  INV-24  ", "warehouse_code": "central",
                "movement_type": "RECEIVING_IN", "date_from": "2026-09-01", "date_to": "2026-09-24",
                "page": 2, "page_size": 25, "snapshot_id": 87})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), expected)
            self.assertEqual(response.headers.get("cache-control"), "no-store")
            listing.assert_awaited_once_with(self.db, q="INV-24", sku="Case-SKU-01", warehouse_code="central",
                movement_type=MovementType.RECEIVING_IN, date_from=date(2026, 9, 1), date_to=date(2026, 9, 24),
                page=2, page_size=25, snapshot_id=87, tracking_scope="all")
            upstream.assert_not_called()
        self.db.commit.assert_not_awaited()
        self.db.flush.assert_not_awaited()

    def test_options_are_protected_local_read_with_no_store(self):
        expected = {"warehouses": [], "movement_types": [], "date_timezone": "UTC"}
        with patch.object(service, "options", AsyncMock(return_value=expected)) as options:
            response = self.client.get("/stock-history/options", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), expected)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        options.assert_awaited_once_with(self.db)

    def test_invalid_ranges_types_and_unbounded_queries_do_not_reach_service(self):
        invalid = ({"page": 0}, {"page": 1000001}, {"page_size": 24}, {"page_size": 26},
            {"page_size": 101}, {"snapshot_id": -1}, {"snapshot_id": 9223372036854775808},
            {"movement_type": "made_up"}, {"date_from": "not-a-date"}, {"date_to": "9999-12-31"},
            {"date_from": "2026-09-24", "date_to": "2026-09-23"}, {"q": "x" * 201},
            {"sku": "x" * 101}, {"warehouse_code": "x" * 51})
        with patch.object(service, "list_movements", AsyncMock()) as listing:
            for params in invalid:
                with self.subTest(params=params):
                    response = self.client.get("/stock-history/movements", params=params, headers=self.headers)
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertEqual(response.headers.get("cache-control"), "no-store")
            listing.assert_not_awaited()


class StockHistoryPresentationTests(IsolatedAsyncioTestCase):
    async def test_unknown_cost_remains_null_and_signed_quantity_reconstructs_before_balance(self):
        class Row(SimpleNamespace):
            def __getitem__(self, index):
                if index != 0:
                    raise IndexError(index)
                return self.movement

        rows = []
        for identifier, quantity, after, cost in ((1, "5", "5", None), (2, "-2", "3", "0")):
            movement = SimpleNamespace(id=identifier, product_id=1,
                movement_type=MovementType.RECEIVING_IN if identifier == 1 else MovementType.SALE_OUT,
                quantity=D(quantity), balance_after=D(after), unit_cost=D(cost) if cost is not None else None,
                total_cost=D("0") if cost is not None else None, notes="Physical stock", reference_type=None,
                reference_id=None, reference_source=None, created_by="operator",
                created_at=datetime(2026, 9, 24, tzinfo=timezone.utc))
            rows.append(Row(movement=movement, sku="SKU-1", product_name="Product", warehouse_code="central",
                warehouse_name="Central", reference_label=None, document=None, shop_code=None, supplier_code=None,
                current_inventory=identifier == 2))
        db = SimpleNamespace(scalar=AsyncMock(side_effect=[2, 2]),
            execute=AsyncMock(return_value=SimpleNamespace(all=lambda: rows)))
        with patch.object(UpgatesClient, "from_shop") as upstream:
            result = await service.list_movements(db)
            upstream.assert_not_called()
        incoming, outgoing = result["items"]
        self.assertEqual((incoming["tracking_scope"], outgoing["tracking_scope"]), ("historical", "current"))
        self.assertIsNone(incoming["unit_cost"])
        self.assertIsNone(incoming["total_cost"])
        self.assertEqual((incoming["balance_before"], incoming["balance_after"]), ("0", "5"))
        self.assertEqual((outgoing["quantity"], outgoing["balance_before"], outgoing["balance_after"]), ("-2", "5", "3"))
        self.assertEqual((outgoing["unit_cost"], outgoing["total_cost"]), ("0", "0"))
        self.assertEqual(outgoing["cost_basis"], "recorded_at_movement")
        self.assertEqual((result["snapshot_id"], result["upgates_calls"], result["source"]), (2, 0, "hub"))

    async def test_empty_ledger_uses_zero_snapshot_and_queries_compile_for_postgres(self):
        class DB:
            async def scalar(self, statement):
                statement.compile(dialect=postgresql.dialect())
                return None

            async def execute(self, statement):
                statement.compile(dialect=postgresql.dialect())
                return SimpleNamespace(all=lambda: [])

        result = await service.list_movements(DB(), sku="Exact-SKU", q="INV_%", warehouse_code="central",
            movement_type=MovementType.RECEIVING_IN, date_from=date(2026, 9, 1), date_to=date(2026, 9, 24))
        self.assertEqual((result["items"], result["snapshot_id"], result["total"]), ([], 0, 0))
