"""Public confirmation and recovery contracts for issued-order reviews."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.order_exception_types import ResolutionConfirmRequest, ResolutionPreviewRequest
from inventory_hub.routers import order_processing as routes
from inventory_hub.services import order_processing


class ExceptionInputTests(unittest.TestCase):
    def test_only_explicit_accounting_confirmation_and_reason_are_accepted(self):
        body = {"preview_hash": "a" * 64, "confirmed": True, "stock_accounted": True, "reason": "Checked issued stock"}
        ResolutionConfirmRequest(**body)
        for field in ("confirmed", "stock_accounted"):
            for invalid in (False, 1, "true", None):
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValidationError):
                    ResolutionConfirmRequest(**{**body, field: invalid})
        for extra in ({"quantity": "1"}, {"physical_received": True}, {"actor": "admin"}, {"reason": "  "}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                ResolutionConfirmRequest(**{**body, **extra})
        with self.assertRaises(ValidationError):
            ResolutionPreviewRequest(request_id=uuid4(), source_hash="forged")

    def test_confirmation_actor_comes_from_authenticated_request_and_recovery_uses_uuid(self):
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[operator_access] = lambda: None
        app.dependency_overrides[get_session] = lambda: object()

        @app.middleware("http")
        async def identity(request, call_next):
            request.state.hub_user = {"id": 42}
            return await call_next(request)

        identifier = str(uuid4())
        with TestClient(app) as client, patch.object(routes.order_exceptions, "resolve", AsyncMock(return_value={"ok": True})) as resolve, \
                patch.object(routes.order_exceptions, "get_preview", AsyncMock(return_value={"id": identifier})) as recover:
            response = client.post(f"/order-processing/resolutions/{identifier}/resolve", json={
                "preview_hash": "a" * 64, "confirmed": True, "stock_accounted": True, "reason": "Checked ledger"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(resolve.await_args.args[-1], "user:42")
            self.assertEqual(client.get(f"/order-processing/resolutions/{identifier}").json(), {"id": identifier})
            self.assertEqual(recover.await_args.args[1], identifier)


class ExceptionJobsTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_jobs_stay_readable_before_stock_policy_is_configured(self):
        db = SimpleNamespace(scalar=AsyncMock(side_effect=[SimpleNamespace(id=1), 0]),
                             scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [])))
        with patch.object(order_processing.stock_settings, "effective", AsyncMock()) as settings:
            self.assertEqual((await order_processing.jobs(db, "biketrek"))["jobs"], [])
        settings.assert_not_awaited()
