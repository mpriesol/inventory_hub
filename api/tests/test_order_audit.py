"""Protected order sample, identity and privacy contracts. No live Upgates calls."""
import asyncio
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
import requests
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.database import get_session
from inventory_hub.db_models import IdentifierType, Product, ProductIdentifier, Shop
from inventory_hub.db_models_ext import ShopProduct
from inventory_hub.routers import order_audit as router_module
from inventory_hub.services import order_audit as audit
from inventory_hub.services.product_identity import IdentityIndex
from inventory_hub.services.upgates import UpgatesClient, UpgatesError
from inventory_hub.settings import settings


NOW = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
STATUSES = {"order_statuses": [
    {"id": 1, "type": "Received", "descriptions": [{"language_id": "sk", "name": "Prijatá"}]},
    {"id": 2, "type": "Canceled", "descriptions": [{"language_id": "sk", "name": "Storno"}]},
    {"id": 8, "type": "Sent", "descriptions": [{"language_id": "sk", "name": "Odoslaná"}]},
    {"id": 19, "type": "Custom", "descriptions": [{"language_id": "sk", "name": "Vyzdvihnutá"}]},
    {"id": 25, "type": "Custom", "descriptions": [{"language_id": "sk", "name": "Reklamácia"}]},
]}


def line(**values):
    return {"uuid": uuid4().hex, "code": "REMOTE-A", "title": "Product A", "quantity": "1.500", "unit": "m", "type": "product", **values}


def order(*lines, **values):
    return {"order_number": "TEST-01", "origin": "eshop", "creation_time": NOW.isoformat(),
            "last_update_time": NOW.isoformat(), "status_id": 1, "status": "Prijatá", "resolved_yn": False,
            "products": list(lines) or [line()], **values}


def page(*orders):
    return {"current_page": 1, "number_of_pages": 1, "number_of_items": len(orders), "orders": list(orders)}


def identity_index():
    products = [Product(id=1, sku="LOCAL-A", name="A"), Product(id=2, sku="LOCAL-B", name="B"),
                Product(id=3, sku="UNMAPPED", name="Unmapped")]
    mappings = [ShopProduct(id=1, shop_id=1, product_id=1, external_code="REMOTE-A", is_variant=False),
                ShopProduct(id=2, shop_id=1, product_id=2, external_code="PARENT", parent_code="PARENT",
                            variant_code="BLUE-M", is_variant=True)]
    identifiers = [ProductIdentifier(id=1, product_id=1, value="4006381333931", identifier_type=IdentifierType.ean),
                   ProductIdentifier(id=2, product_id=3, value="5901234123457", identifier_type=IdentifierType.ean)]
    return IdentityIndex(1, products, mappings, identifiers)


class OrderNormalizationTests(unittest.IsolatedAsyncioTestCase):
    async def normalize(self, *orders, index=None):
        with patch.object(audit, "load_identity_index", AsyncMock(return_value=index or identity_index())):
            return await audit.normalize_order_page(None, Shop(id=1, code="biketrek"), page(*orders), STATUSES,
                                                    page=1, fetched_at=NOW)

    async def test_manual_lines_are_excluded_without_blocking_mapped_lines(self):
        result = await self.normalize(order(line(), line(code="", title="Custom workshop line")))
        self.assertEqual([row["classification"] for row in result["orders"][0]["lines"]], ["mapped", "manual"])
        self.assertEqual(result["summary"]["manual"], 1)
        self.assertEqual(result["summary"]["mapped"], 1)
        self.assertEqual(result["orders"][0]["candidate"], "reserve")
        self.assertEqual(result["orders"][0]["lines"][0]["quantity"], "1.500")

    async def test_variant_leaf_code_is_used_not_parent_or_native_product_id(self):
        result = await self.normalize(order(line(code="BLUE-M", product_id=999, option_set_id=100)))
        row = result["orders"][0]["lines"][0]
        self.assertEqual((row["classification"], row["product_id"], row["sku"]), ("mapped", 2, "LOCAL-B"))
        parent = await self.normalize(order(line(code="PARENT", product_id=999)))
        self.assertEqual(parent["orders"][0]["lines"][0]["classification"], "unresolved")

    async def test_unmapped_code_and_code_less_real_identity_are_not_manual(self):
        result = await self.normalize(order(line(code="UNKNOWN"), line(code="", product_id=5),
                                            line(code="", ean="5901234123457"), line(code="UNMAPPED")))
        rows = result["orders"][0]["lines"]
        self.assertEqual([row["classification"] for row in rows], ["unresolved", "unresolved", "identified", "conflict"])
        self.assertIn("identity_review_required", result["orders"][0]["warnings"])

    async def test_bad_identity_cannot_match_a_truncated_prefix(self):
        result = await self.normalize(order(line(code="REMOTE-A\n" + "X" * 101), line(code={"code": "REMOTE-A"}),
                                            line(code="", product_id={}), line(code="", option_set_id="bad")))
        for row in result["orders"][0]["lines"]:
            self.assertEqual(row["classification"], "conflict")
            self.assertIn("invalid_identity", row["reasons"])
            self.assertIsNone(row["product_id"])

    async def test_related_types_and_invalid_quantity_stay_visible_for_review(self):
        result = await self.normalize(order(line(type="set"), line(type="set_part"), line(type="gift"),
                                            line(type="discount"), line(quantity="NaN"), line(quantity="0.0001")))
        self.assertEqual(result["summary"]["conflict"], 4)
        self.assertEqual(result["summary"]["mapped"], 1)
        self.assertEqual(result["summary"]["non_stock"], 1)
        self.assertIsNone(result["orders"][0]["lines"][-1]["quantity"])

    async def test_discount_quantity_is_not_stock_and_gift_has_real_identity(self):
        result = await self.normalize(order(line(type="discount", code="", quantity="-1"),
                                            line(type="gift", parent_uuid="purchased-line")))
        discount, gift = result["orders"][0]["lines"]
        self.assertEqual(discount["classification"], "non_stock")
        self.assertNotIn("invalid_quantity", discount["reasons"])
        self.assertEqual((gift["classification"], gift["product_id"]), ("mapped", 1))

    async def test_units_are_warned_not_assumed_convertible_to_stock_units(self):
        result = await self.normalize(order(line(unit="m"), line(unit="ks"), line(code="", unit="hod")))
        rows = result["orders"][0]["lines"]
        self.assertIn("unit_requires_review", rows[0]["reasons"])
        self.assertNotIn("unit_requires_review", rows[1]["reasons"])
        self.assertNotIn("unit_requires_review", rows[2]["reasons"])
        self.assertIn("unit_requires_review", result["orders"][0]["warnings"])

    async def test_duplicate_and_missing_uuid_warn_without_collapsing_rows(self):
        result = await self.normalize(order(line(uuid="same"), line(uuid="same"), line(uuid="")))
        rows = result["orders"][0]["lines"]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1]["classification"], "conflict")
        self.assertIn("duplicate_line_uuid", result["orders"][0]["warnings"])
        self.assertIn("missing_line_uuid", result["orders"][0]["warnings"])

    async def test_candidate_is_not_payment_or_unknown_status_guess(self):
        cases = [(order(status_id=8), "issue"), (order(status_id=19), "issue"),
                 (order(status_id=1, paid_date="2026-09-23"), "reserve"),
                 (order(status_id=2, origin="cash-register", paid_date="2026-09-23", resolved_yn=True), "cancel"),
                 (order(origin="cash-register", paid_date="2026-09-23", resolved_yn=True), "issue"),
                 (order(status_id=999, status="Odoslaná", delivered_date="2026-09-23"), "review"),
                 (order(status_id=25), "review"),
                 (order(status_id=25, origin="cash-register", paid_date="2026-09-23", resolved_yn=True), "review")]
        for source, expected in cases:
            with self.subTest(expected=expected, source=source["status_id"]):
                result = await self.normalize(source)
                self.assertEqual(result["orders"][0]["candidate"], expected)

    async def test_response_is_explicit_projection_without_customer_or_raw_fields(self):
        marker = "PRIVATE-CUSTOMER-SECRET"
        source = order(line(buy_price=marker, invoice_info=marker, configurations=[{"value": marker}], image_url=marker),
                       customer={"email": marker, "street_invoice": marker}, internal_note=marker,
                       admin_url=marker, tracking_code=marker, attachments=[{"url": marker}], metas=[marker], order_total=marker)
        result = await self.normalize(source)
        rendered = json.dumps(result)
        self.assertNotIn(marker, rendered)
        self.assertTrue(result["read_only"])
        self.assertNotIn("locked", rendered)
        self.assertEqual(result["warnings"], ["snapshot_not_stock_history"])

    async def test_page_is_bounded_and_incomplete_metadata_is_not_invented(self):
        for payload in (page(*[order() for _ in range(101)]), {"orders": [order()]}, page(order(products={})),
                        page(order(order_number="X" * 101)), page(order(), order())):
            with self.subTest(payload_type=type(payload)), patch.object(audit, "load_identity_index", AsyncMock(return_value=identity_index())):
                with self.assertRaises(audit.OrderAuditError):
                    await audit.normalize_order_page(None, Shop(id=1, code="biketrek"), payload, STATUSES, page=1, fetched_at=NOW)

    async def test_malformed_status_language_is_safe_error(self):
        malformed = {"order_statuses": [{"id": 1, "type": "Received", "descriptions": [{"language_id": []}]}]}
        with self.assertRaisesRegex(audit.OrderAuditError, "order_audit_invalid_response"):
            await audit.normalize_order_page(None, Shop(id=1, code="biketrek"), page(order()), malformed, page=1, fetched_at=NOW)


class OrderTransportTests(unittest.TestCase):
    def response(self, payload, status=200):
        response = Mock(status_code=status, content=b"{}")
        response.json.return_value = payload
        return response

    def client(self):
        client = UpgatesClient("https://example.invalid/api/v2", "test", "test")
        client.session = Mock()
        return client

    def test_two_gets_no_retry_raw_logs_or_remote_writes(self):
        client = self.client()
        client.session.get.side_effect = [self.response(page(order())), self.response(STATUSES)]
        params = {"page": 1, "order_by": "creation_time", "order_dir": "desc"}
        with patch.object(client, "_log") as log:
            orders, statuses = client.read_order_audit(params)
        self.assertEqual(client.session.get.call_count, 2)
        self.assertTrue(client.session.get.call_args_list[0].args[0].endswith("/orders"))
        self.assertTrue(client.session.get.call_args_list[1].args[0].endswith("/order-statuses"))
        self.assertFalse(client.session.get.call_args_list[0].kwargs["allow_redirects"])
        client.session.post.assert_not_called()
        client.session.put.assert_not_called()
        log.assert_not_called()
        self.assertEqual(statuses, STATUSES)

    def test_auth_errors_and_body_messages_never_leak_or_retry(self):
        for response in (self.response({"private": "CUSTOMER"}, 401), self.response({"messages": ["CUSTOMER"]}),
                         self.response(["CUSTOMER"])):
            client = self.client()
            client.session.get.return_value = response
            with patch.object(client, "_log") as log, self.assertRaises(UpgatesError) as error:
                client.read_order_audit({"page": 1})
            self.assertNotIn("CUSTOMER", str(error.exception))
            self.assertEqual(client.session.get.call_count, 1)
            log.assert_not_called()
            response.close.assert_called_once()

    def test_network_error_is_sanitized_and_fetch_uses_fixed_single_page(self):
        client = self.client()
        client.session.get.side_effect = requests.ConnectionError("PRIVATE URL OR KEY")
        with patch.object(UpgatesClient, "from_shop", return_value=client), self.assertRaises(audit.OrderAuditError) as error:
            audit._fetch_page("biketrek", 7, 2, NOW)
        self.assertEqual(str(error.exception), "order_audit_upgates_unavailable")
        params = client.session.get.call_args.kwargs["params"]
        self.assertEqual((params["page"], params["order_by"], params["order_dir"]), (2, "creation_time", "desc"))
        self.assertNotIn("current_page_items", params)  # Not documented as an orders request parameter.
        client.session.close.assert_called_once()


class ProtectedOrderRouteTests(unittest.TestCase):
    def setUp(self):
        self.db = SimpleNamespace(execute=AsyncMock())
        self.dependency_calls = []

        async def db_session():
            self.dependency_calls.append(True)
            yield self.db

        app = FastAPI()
        app.include_router(router_module.router)
        app.dependency_overrides[get_session] = db_session
        self.client = TestClient(app)
        self.token = "test-only-operator-access-" + "x" * 24
        self.access_patch = patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr(self.token))
        self.access_patch.start()

    def tearDown(self):
        self.access_patch.stop()
        self.client.close()

    def test_unauthorized_read_rejects_before_db_or_network(self):
        with patch.object(router_module, "audit_orders", AsyncMock()) as service:
            response = self.client.get("/shops/biketrek/upgates/orders/audit")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.dependency_calls, [])
        service.assert_not_called()

    def test_authorized_query_is_read_only_no_store_and_exact_days(self):
        with patch.object(router_module, "audit_orders", AsyncMock(return_value={"read_only": True})) as service:
            response = self.client.get("/shops/biketrek/upgates/orders/audit?days=7&page=2",
                                       headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(str(self.db.execute.call_args.args[0]), "SET TRANSACTION READ ONLY")
        service.assert_awaited_once_with(self.db, "biketrek", 7, 2)

    def test_invalid_page_or_days_does_not_fetch_upstream(self):
        with patch.object(router_module, "audit_orders", AsyncMock()) as service:
            for query in ("days=1", "days=365", "page=0", "page=1001"):
                response = self.client.get("/shops/biketrek/upgates/orders/audit?" + query,
                                           headers={"Authorization": f"Bearer {self.token}"})
                self.assertEqual(response.status_code, 422)
        service.assert_not_called()


TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")


@unittest.skipUnless(TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class OrderAuditDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("Order audit tests require a dedicated localhost *_catalog_test database")
        self.schema = "order_audit_test_" + uuid4().hex
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            root = Path(__file__).resolve().parents[2]
            await connection.execute((root / "infra/db-init/001_schema.sql").read_text())
        finally:
            await connection.close()
        self.engine = create_async_engine(TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1),
                                         poolclass=NullPool, connect_args={"server_settings": {"search_path": self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        async with self.sessions() as db:
            self.shop_id = (await db.execute(text("SELECT id FROM shops WHERE code='biketrek'"))).scalar_one()
            product = Product(sku="LOCAL-A", name="Test stock product")
            db.add(product)
            await db.flush()
            db.add(ShopProduct(shop_id=self.shop_id, product_id=product.id, external_code="REMOTE-A", is_variant=False))
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            await connection.close()

    async def test_mixed_sample_resolves_under_read_only_transaction_without_business_writes(self):
        async with self.sessions() as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            with patch.object(audit, "_fetch_page", return_value=(page(order(line(), line(code=""), line(code="UNKNOWN"))), STATUSES)):
                result = await audit.audit_orders(db, "biketrek", 30, 1)
            self.assertEqual((result["summary"]["mapped"], result["summary"]["manual"], result["summary"]["unresolved"]), (1, 1, 1))
            for table in ("stock_movements", "stock_balances", "shop_orders", "shop_order_items", "reservations"):
                count = (await db.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
                self.assertEqual(count, 0, table)
            product_count = (await db.execute(text("SELECT count(*) FROM products"))).scalar_one()
            self.assertEqual(product_count, 1)
