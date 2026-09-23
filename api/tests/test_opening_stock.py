"""Opening-stock parsing and protected HTTP contracts; no live DB or network."""
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from inventory_hub.database import get_session
from inventory_hub.opening_stock_types import OpeningFinalizeRequest, OpeningPreviewRequest
from inventory_hub.routers import opening_stock as routes
from inventory_hub.services import opening_stock as opening
from inventory_hub.settings import settings


HEADER = "sku;quantity;unit_cost;unit\n"
NOW = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)


def preview_body(**changes):
    return {"request_id": str(uuid4()), "warehouse_code": "opening-test",
            "source_reference": "Physical count sheet 42", "operator_name": "Test operator",
            "counted_at": NOW.isoformat(), "csv_text": HEADER + "EXACT-SKU;2;10;ks\n", **changes}


class OpeningParserTests(unittest.TestCase):
    def test_exact_decimal_values_supported_delimiters_and_explicit_zero_cost_warning(self):
        for csv_text in (
            HEADER + "SKU-A;2.000;12,3456;ks\nSKU-B;1;0;ks\n",
            'sku,quantity,unit_cost,unit\nSKU-A,2,"12,3456",ks\nSKU-B,1,0,ks\n',
            "sku\tquantity\tunit_cost\tunit\nSKU-A\t2\t12.3456\tks\nSKU-B\t1\t0\tks\n",
        ):
            with self.subTest(delimiter=csv_text.splitlines()[0]):
                rows, errors, warnings = opening.parse_csv(csv_text)
                self.assertEqual(errors, [])
                self.assertEqual((rows[0]["quantity"], rows[0]["unit_cost"], rows[0]["value"]),
                                 (Decimal("2"), Decimal("12.3456"), Decimal("24.6912")))
                self.assertEqual(rows[1]["unit_cost"], Decimal("0"))
                self.assertTrue(warnings, "A deliberately entered zero acquisition cost needs a visible warning")
                self.assertTrue(all(row["unit"] == "ks" for row in rows))

    def test_quantity_and_cost_never_accept_nonfinite_exponent_boolean_or_grouping_notation(self):
        quantities = ("", "0", "-1", "1.5", "1e2", "NaN", "Infinity", "true", "false", "1 000", "1,000.00")
        costs = ("", "-1", "NaN", "Infinity", "1e2", "true", "false", "1 000", "1,000.00", "1.000,00", "1'000", "1.00001")
        for field, values in (("quantity", quantities), ("unit_cost", costs)):
            for value in values:
                with self.subTest(field=field, value=value):
                    fields = {"sku": "SKU-A", "quantity": "2", "unit_cost": "10", "unit": "ks", field: value}
                    _, errors, _ = opening.parse_csv(HEADER + ";".join(fields.values()) + "\n")
                    self.assertTrue(errors)

    def test_required_unit_prices_and_exact_headers_are_not_guessed(self):
        cases = (
            "sku;quantity;unit_cost\nSKU-A;2;10\n",
            "sku;quantity;unit_cost;unit;price_sale\nSKU-A;2;10;ks;20\n",
            "sku;quantity;unit_cost;unit;sku\nSKU-A;2;10;ks;OTHER\n",
            HEADER + "SKU-A;2;;ks\n",
            HEADER + "SKU-A;2;10;\n",
            HEADER + "SKU-A;2;10;m\n",
            HEADER + "SKU-A;2;10;kg\n",
            HEADER + ";2;10;ks\n",
            HEADER + "SKU-A;2;10;ks;EXTRA\n",
        )
        for csv_text in cases:
            with self.subTest(csv=csv_text):
                _, errors, _ = opening.parse_csv(csv_text)
                self.assertTrue(errors)

    def test_duplicates_size_row_count_and_database_numeric_limits_are_enforced(self):
        cases = (
            HEADER + "SKU-A;2;10;ks\nSKU-A;3;10;ks\n",
            HEADER + "X" * 1_100_000,
            HEADER + "".join(f"SKU-{i};1;1;ks\n" for i in range(5001)),
            HEADER + "SKU-A;1000000000;1;ks\n",
            HEADER + "SKU-A;1;100000000;ks\n",
            HEADER + "SKU-A;999999999;1001;ks\n",
        )
        for csv_text in cases:
            with self.subTest(size=len(csv_text)):
                _, errors, _ = opening.parse_csv(csv_text)
                self.assertTrue(errors)


class OpeningInputTests(unittest.TestCase):
    def test_preview_requires_explicit_audit_metadata_and_timezone(self):
        valid = preview_body()
        OpeningPreviewRequest(**valid)
        for field in ("request_id", "warehouse_code", "source_reference", "operator_name", "counted_at", "csv_text"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                OpeningPreviewRequest(**{key: value for key, value in valid.items() if key != field})
        with self.assertRaises(ValidationError):
            OpeningPreviewRequest(**preview_body(counted_at="2026-09-23T10:00:00"))
        for field in ("warehouse_code", "source_reference", "operator_name"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                OpeningPreviewRequest(**preview_body(**{field: "invalid\ud800text"}))

    def test_finalize_requires_exact_hash_and_real_confirmation_booleans_no_replacement_lines(self):
        valid = {"preview_hash": "a" * 64, "confirmed": True, "receipts_reconciled": True}
        OpeningFinalizeRequest(**valid)
        for field in ("confirmed", "receipts_reconciled"):
            for value in (False, None, "true", 1):
                with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                    OpeningFinalizeRequest(**{**valid, field: value})
        for change in ({"preview_hash": "bad"}, {"quantity": "999"}, {"rows": []}, {"warehouse_code": "OTHER"}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                OpeningFinalizeRequest(**{**valid, **change})


class OpeningPreviewValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_unicode_csv_is_rejected_before_hashing_or_database_access(self):
        db = SimpleNamespace(get=AsyncMock(), execute=AsyncMock(), commit=AsyncMock())
        payload = OpeningPreviewRequest(**preview_body(csv_text=HEADER + "BAD\ud800SKU;2;10;ks\n"))
        response = await opening.preview(db, payload)
        self.assertFalse(response["ready"])
        self.assertEqual(response["errors"][0]["code"], "csv_invalid")
        self.assertIsNone(response["batch"])
        db.get.assert_not_awaited()
        db.execute.assert_not_awaited()
        db.commit.assert_not_awaited()


class OpeningProtectedRouteTests(unittest.TestCase):
    def setUp(self):
        self.db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
        self.dependency_calls = []

        async def session():
            self.dependency_calls.append(True)
            yield self.db

        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[get_session] = session
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.token = "test-only-opening-stock-token-" + "x" * 24
        self.headers = {"Authorization": f"Bearer {self.token}"}
        token_patch = patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr(self.token))
        token_patch.start()
        self.addCleanup(token_patch.stop)

    def test_all_routes_reject_unauthorized_calls_before_database_or_service(self):
        batch_id = str(uuid4())
        calls = [("GET", "/stock/opening/options", None), ("GET", "/stock/opening/batches", None),
                 ("GET", f"/stock/opening/{batch_id}", None),
                 ("POST", "/stock/opening/preview", preview_body()),
                 ("POST", f"/stock/opening/{batch_id}/finalize", {"preview_hash": "a" * 64,
                      "confirmed": True, "receipts_reconciled": True})]
        with patch.object(opening, "preview", AsyncMock()) as preview, patch.object(opening, "finalize", AsyncMock()) as finalize:
            for method, path, body in calls:
                response = self.client.request(method, path, json=body)
                self.assertEqual(response.status_code, 401, response.text)
            self.assertEqual(self.dependency_calls, [])
            preview.assert_not_awaited()
            finalize.assert_not_awaited()

    def test_invalid_request_has_no_stock_operation_and_valid_response_is_not_cached(self):
        with patch.object(opening, "finalize", AsyncMock()) as finalize:
            response = self.client.post(f"/stock/opening/{uuid4()}/finalize", headers=self.headers,
                json={"preview_hash": "a" * 64, "confirmed": False, "receipts_reconciled": True})
            self.assertEqual(response.status_code, 422)
            finalize.assert_not_awaited()
            self.db.execute.assert_not_awaited()
            self.db.commit.assert_not_awaited()
        with patch.object(opening, "preview", AsyncMock(return_value={"ready": False, "errors": [], "warnings": [], "batch": None})) as preview:
            response = self.client.post("/stock/opening/preview", headers=self.headers, json=preview_body())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers.get("cache-control"), "no-store")
            preview.assert_awaited_once()

    def test_invalid_unicode_audit_metadata_returns_safe_validation_error_without_service_write(self):
        import json

        with patch.object(opening, "preview", AsyncMock()) as preview:
            response = self.client.post("/stock/opening/preview", headers={**self.headers, "Content-Type": "application/json"},
                content=json.dumps(preview_body(operator_name="private\ud800operator"), ensure_ascii=True))
        self.assertEqual(response.status_code, 422, response.text)
        self.assertNotIn("private", response.text)
        preview.assert_not_awaited()
        self.db.execute.assert_not_awaited()
        self.db.commit.assert_not_awaited()
