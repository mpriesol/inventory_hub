"""Invoice records and settlement actions stay private and reject unsafe retries."""
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from inventory_hub.database import get_session
from inventory_hub.routers import invoice_archive as archive_routes
from inventory_hub.routers import invoices_unified as routes
from inventory_hub.routers import shop_invoice_sync as shop_routes
from inventory_hub.services import invoice_archive as archive
from inventory_hub.services import invoice_finance as finance
from inventory_hub.services import shop_invoice_sync
from inventory_hub.services.shop_invoice_source import SourceError
from inventory_hub.settings import settings


class InvoiceRouteTests(unittest.TestCase):
    def setUp(self):
        self.sessions = []
        self.db = AsyncMock()

        async def session():
            self.sessions.append(True)
            yield self.db

        app = FastAPI()
        app.include_router(routes.router)
        app.include_router(archive_routes.router)
        app.include_router(shop_routes.router)
        app.dependency_overrides[get_session] = session
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        token = "invoice-route-fixture-" + "x" * 32
        self.enterContext(patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr(token)))
        self.headers = {"Authorization": "Bearer " + token}
        self.request_id = "11111111-1111-4111-8111-111111111111"
        self.payment = {"request_id": self.request_id, "expected_revision": 0,
                        "amount": "30.01", "kind": "payment", "paid_at": "2026-02-01T12:30:00Z",
                        "note": "Synthetic operator note"}

    def test_every_archive_and_finance_route_authenticates_before_database(self):
        requests = [
            ("GET", "/invoices", None), ("GET", "/invoices/suppliers", None),
            ("GET", "/invoices/supplier-options", None),
            ("GET", "/invoices/stats", None), ("GET", "/invoices/1", None),
            ("GET", "/invoices/1/download", None),
            ("GET", f"/invoices/1/payment-operations/{self.request_id}", None),
            ("POST", "/invoices/1/payments", self.payment),
            ("POST", "/invoices/1/payments/2/reverse", {"request_id": self.request_id, "expected_revision": 1}),
            ("PATCH", "/invoices/1", {"expected_revision": 0, "notes": "Synthetic note"}),
            ("PATCH", "/invoices/1/payment", {"payment_status": "paid"}),
            ("DELETE", "/invoices/1", None),
            ("POST", "/invoices/reconcile", {"supplier_code": "archive-fixture"}),
            ("POST", "/invoices/sync/shop/biketrek", {"date_from": "2026-10-01", "date_to": "2026-10-23"}),
        ]
        with ExitStack() as stack:
            calls = [stack.enter_context(patch.object(service, name, AsyncMock())) for service, name in (
                (finance, "apply_payment"), (finance, "operation_result"), (finance, "update_metadata"),
                (archive, "reconcile_supplier"),
                (shop_invoice_sync, "refresh"),
            )]
            for method, path, payload in requests:
                with self.subTest(method=method, path=path):
                    response = self.client.request(method, path, json=payload)
                    self.assertEqual(response.status_code, 401, response.text)
                    self.assertEqual(response.headers["cache-control"], "no-store")
            upload = self.client.post("/invoices/upload", data={"supplier_code": "archive-fixture"},
                                      files={"file": ("fixture.pdf", b"synthetic invoice", "application/pdf")})
            self.assertEqual(upload.status_code, 401)
            self.assertEqual(upload.headers["cache-control"], "no-store")
            self.assertEqual(self.sessions, [])
            self.db.execute.assert_not_awaited()
            for action in calls:
                action.assert_not_awaited()

    def test_shop_source_http_error_exposes_only_status_operation_and_stable_code(self):
        for status in (400, 403, 429):
            code = 'invoice_source_rate_limited' if status == 429 else 'invoice_source_http_failed'
            error = SourceError(code, 429 if status == 429 else 502, 37 if status == 429 else None,
                                upstream_status=status, operation='list', rejected_parameters=['creation_time_to', 'private'])
            error.url = 'https://private.invalid/PRIVATE-INVOICE-001'
            error.body = 'private customer and credential details'
            with self.subTest(status=status), patch.object(shop_invoice_sync, 'refresh', AsyncMock(side_effect=error)):
                response = self.client.post('/invoices/sync/shop/biketrek', headers=self.headers,
                    json={'date_from': '2026-10-01', 'date_to': '2026-10-23'})
                self.assertEqual(response.status_code, 429 if status == 429 else 502)
                expected = {'code': code, 'upstream_status': status, 'operation': 'list'}
                if status == 400:
                    expected['rejected_parameters'] = ['creation_time_to']
                self.assertEqual(response.json(), {'detail': expected})
                self.assertEqual(response.headers['cache-control'], 'no-store')
                if status == 429:
                    self.assertEqual(response.headers['retry-after'], '37')
                self.assertNotIn('private', response.text)

    def test_payment_and_reversal_use_typed_requests_and_authenticated_actor(self):
        with patch.object(finance, "apply_payment", AsyncMock(return_value={"ok": True})) as action:
            response = self.client.post("/invoices/7/payments", json=self.payment, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
            args, kwargs = action.await_args
            self.assertEqual(args[:2], (self.db, 7))
            self.assertIsInstance(args[2], finance.PaymentRequest)
            self.assertEqual((args[2].request_id, args[2].amount), (UUID(self.request_id), "30.01"))
            self.assertEqual(kwargs, {"actor": "operator-token"})
        self.db.commit.assert_awaited_once()
        with patch.object(finance, "apply_payment", AsyncMock(return_value={"ok": True})) as action:
            response = self.client.post("/invoices/7/payments/9/reverse", json={
                "request_id": self.request_id, "expected_revision": 1, "note": "Correct posting",
            }, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)
            args, kwargs = action.await_args
            self.assertIsInstance(args[2], finance.ReversalRequest)
            self.assertEqual(kwargs, {"actor": "operator-token", "reverses_payment_id": 9})

    def test_recovery_returns_saved_result_and_invalid_uuid_never_calls_service(self):
        stored = {"invoice_id": 7, "request_id": self.request_id, "payment_revision": 1}
        with patch.object(finance, "operation_result", AsyncMock(return_value=stored)) as action:
            response = self.client.get(f"/invoices/7/payment-operations/{self.request_id}", headers=self.headers)
            self.assertEqual(response.json(), stored)
            self.assertEqual(response.headers["cache-control"], "no-store")
            action.assert_awaited_once_with(self.db, 7, UUID(self.request_id))
        with patch.object(finance, "operation_result", AsyncMock()) as action:
            response = self.client.get("/invoices/7/payment-operations/private-invalid-reference", headers=self.headers)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("private-invalid-reference", response.text)
            action.assert_not_awaited()

    def test_invalid_financial_inputs_are_sanitized_and_never_applied(self):
        for changes in (
            {"amount": 30.01}, {"amount": "NaN"}, {"amount": "0.001"}, {"amount": "-1"},
            {"expected_revision": True}, {"paid_at": "2026-02-01T12:30:00"},
            {"actor": "private-forged-actor"}, {"private_source_document": "sensitive-fixture-document"},
        ):
            with self.subTest(changes=changes), patch.object(finance, "apply_payment", AsyncMock()) as action:
                response = self.client.post("/invoices/1/payments", json={**self.payment, **changes}, headers=self.headers)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertTrue(response.json()["detail"]["known_unapplied"])
                self.assertNotIn("private-forged-actor", response.text)
                self.assertNotIn("sensitive-fixture-document", response.text)
                action.assert_not_awaited()

    def test_revision_or_replay_conflict_is_visible_and_transaction_rolls_back(self):
        for code in ("invoice_payment_revision_changed", "invoice_payment_request_conflict"):
            with self.subTest(code=code), patch.object(finance, "apply_payment", AsyncMock(
                    side_effect=finance.FinanceError(code))):
                response = self.client.post("/invoices/1/payments", json=self.payment, headers=self.headers)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["detail"], {"code": code,
                    "known_unapplied": code != "invoice_payment_request_conflict"})
                self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.db.rollback.await_count, 2)
        self.db.commit.assert_not_awaited()

    def test_old_payment_status_patch_cannot_bypass_the_ledger(self):
        response = self.client.patch("/invoices/1/payment", json={"payment_status": "paid", "paid_amount": "100"},
                                     headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "invoice_payment_ledger_required")
        self.db.execute.assert_not_awaited()
        self.db.commit.assert_not_awaited()

    def test_metadata_changes_have_revision_and_reconciliation_is_bounded(self):
        with patch.object(finance, "update_metadata", AsyncMock(return_value={"ok": True})) as action:
            response = self.client.patch("/invoices/7", json={"expected_revision": 3, "notes": "Correct note"},
                                         headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)
            action.assert_awaited_once_with(self.db, 7, {"notes": "Correct note"}, 3)
        with patch.object(archive, "reconcile_supplier", AsyncMock(return_value={"created": 1})) as action:
            response = self.client.post("/invoices/reconcile", json={"supplier_code": "archive-fixture", "limit": 50},
                                        headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(action.await_args.args[1].limit, 50)
        for payload in ({"supplier_code": "../private"}, {"supplier_code": "archive-fixture", "limit": 201}):
            with patch.object(archive, "reconcile_supplier", AsyncMock()) as action:
                response = self.client.post("/invoices/reconcile", json=payload, headers=self.headers)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
                action.assert_not_awaited()

    def test_upload_uses_source_lines_and_rejects_conflicting_manual_invoice_number(self):
        self.db.scalar.return_value = SimpleNamespace(id=2, code='sk-profi-bike')
        header = {'invoice_number': '2026/1', 'currency': 'EUR', 'total_with_vat': '12.30'}
        lines = [{'line_number': 1, 'quantity': '1', 'ean': '5901234123457'}]
        with patch.object(archive, 'parse_uploaded_document', return_value=(header.copy(), lines)), \
                patch.object(archive, 'ingest_invoice', AsyncMock(return_value={'id': 7, 'replayed': True})) as ingest:
            response = self.client.post('/invoices/upload', headers=self.headers,
                data={'supplier_code': 'sk-profi-bike'}, files={'file': ('original.pdf', b'%PDF-1.4\n%%EOF')})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(ingest.await_args.kwargs['lines'], lines)
            self.assertEqual(ingest.await_args.kwargs['metadata'], header)
        with patch.object(archive, 'parse_uploaded_document', return_value=(header.copy(), lines)), \
                patch.object(archive, 'ingest_invoice', AsyncMock()) as ingest:
            response = self.client.post('/invoices/upload', headers=self.headers,
                data={'supplier_code': 'sk-profi-bike', 'invoice_number': 'DIFFERENT'},
                files={'file': ('original.pdf', b'%PDF-1.4\n%%EOF')})
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()['detail']['code'], 'invoice_source_metadata_conflict')
            ingest.assert_not_awaited()
