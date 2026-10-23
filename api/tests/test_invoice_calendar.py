"""Calendar reads are private, bounded and keep issue/due-date semantics apart."""
from datetime import date
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from inventory_hub.database import get_session
from inventory_hub.invoice_calendar_types import InvoiceCalendarQuery
from inventory_hub.routers import invoice_calendar as routes
from inventory_hub.services import invoice_calendar as service
from inventory_hub.settings import settings


class InvoiceCalendarTests(unittest.IsolatedAsyncioTestCase):
    def query(self, **changes):
        return InvoiceCalendarQuery.model_validate({"range_from": "2024-01-01", "range_to": "2024-12-31", **changes})

    def test_complete_leap_year_is_allowed_but_longer_or_inverted_periods_are_rejected(self):
        self.assertEqual((self.query().range_to - self.query().range_from).days, 365)
        for changes in ({"range_to": "2025-01-01"}, {"range_to": "2023-12-31"},
                        {"date_from": "2024-03-01", "date_to": "2024-02-29"},
                        {"f_amount_min": "NaN"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.query(**changes)

    def test_filter_conditions_use_bound_values_and_distinguish_issue_dates_from_calendar_due_dates(self):
        where, params = service.calendar_conditions(self.query(supplier_code="vendor%_a",
            date_from="2024-02-01", due_to="2024-05-31", f_supplier="fixture", f_amount_min="0.00"))
        self.assertIn("s.code=:supplier_code", where)
        self.assertNotIn("vendor%_a", where)
        self.assertEqual(params["supplier_code"], "vendor%_a")
        self.assertIn("ui.invoice_date >= :date_from", where)
        self.assertIn("ui.due_date BETWEEN :range_from AND :range_to", where)
        self.assertIn("ui.due_date <= :due_to", where)
        sale_where, _ = service.calendar_conditions(self.query(direction="sale", f_supplier="shop"))
        self.assertIn("sh.name ILIKE", sale_where)

    async def test_overflow_fails_before_returning_a_partial_calendar_or_loading_finance(self):
        headers = [{"id": index} for index in range(service.MAX_CALENDAR_INVOICES + 1)]
        result = SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: headers))
        db = SimpleNamespace(execute=AsyncMock(return_value=result))
        with patch.object(service.finance, "financial_rows", AsyncMock()) as finances:
            with self.assertRaises(service.InvoiceCalendarError) as caught:
                await service.calendar(db, self.query())
        self.assertEqual(caught.exception.code, "invoice_calendar_range_too_large")
        self.assertEqual(db.execute.await_args.args[1]["calendar_limit"], service.MAX_CALENDAR_INVOICES + 1)
        finances.assert_not_awaited()


class InvoiceCalendarRouteTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr("calendar-test-" + "x" * 32)))
        self.db = AsyncMock()
        self.database_called = False

        async def session():
            self.database_called = True
            yield self.db

        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[get_session] = session
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer calendar-test-" + "x" * 32}

    def test_calendar_authenticates_before_database_and_never_caches_errors(self):
        response = self.client.get("/invoices/calendar?range_from=2024-01-01&range_to=2024-12-31")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertFalse(self.database_called)

    def test_valid_calendar_reaches_service_and_invalid_period_is_not_queried(self):
        with patch.object(service, "calendar", AsyncMock(return_value={"items": [], "total": 0})) as read:
            response = self.client.get("/invoices/calendar?range_from=2024-01-01&range_to=2024-12-31&supplier_code=exact-vendor", headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(read.await_args.args[1].supplier_code, "exact-vendor")
            read.reset_mock()
            rejected = self.client.get("/invoices/calendar?range_from=2024-01-01&range_to=2025-01-01", headers=self.headers)
            self.assertEqual(rejected.status_code, 422)
            self.assertEqual(rejected.headers["cache-control"], "no-store")
            read.assert_not_awaited()
