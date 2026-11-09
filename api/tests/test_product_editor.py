"""Manual override ownership, validation and protected editor API."""
from contextlib import ExitStack
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy.dialects import postgresql

from inventory_hub.database import get_session
from inventory_hub.product_editor_types import EditorRowPatch, ProductEditorSaveRequest, ProductCardCreateRequest
from inventory_hub.routers import product_editor as routes
from inventory_hub.services import product_editor as service
from inventory_hub.services.upgates import UpgatesClient
from inventory_hub.settings import settings


def patch_body(**changes):
    return {"product_id": 1, "expected_revision": 0, "snapshot_hash": "a" * 64, "common": {"name": "Edited name"}, **changes}


def save_body(**changes):
    return {"request_id": str(uuid4()), "warehouse_code": None, "confirmed": True, "changes": [patch_body()], **changes}


def facts():
    return {"id": 1, "sku": "SHARED-SKU", "name": "Imported name", "brand": "Imported brand", "is_active": True,
        "group": {"id": 5, "code": "NORMAL", "name": "Normal family"}, "attributes": [{"name": "Size", "value": "10"}],
        "eans": ["0123456789012"], "supplier_codes": [{"supplier_code": "supplier", "code": "CODE-1"}], "image_url": None,
        "shops": [{"shop_code": code, "mapped": True, "mapping": {"parent_code": "POS" if code == "xtrek" else "NORMAL"},
            "observed": {"name": "Observed name", "price": "24.00", "price_basis": "unknown", "visible": True}}
            for code in ("biketrek", "xtrek")]}


class ProductEditorInputTests(TestCase):
    def test_navigation_overrides_accept_only_explicit_safe_http_links(self):
        item = EditorRowPatch(**patch_body(shops={"biketrek": {
            "shop_url": "https://www.example.test/p/new?variant=10#detail", "shop_admin_url": None}}))
        self.assertEqual(item.shops["biketrek"].shop_url, "https://www.example.test/p/new?variant=10#detail")
        self.assertEqual(service._navigation_url("http://supplier.example.test/item"), "http://supplier.example.test/item")
        for value in ("javascript:alert(1)", "data:text/html,<script>", "//example.test/p", "/p/item",
                      "https://user:secret@example.test", "https://@example.test", "https://example.test:wrong/p",
                      "https://example.test\\evil", "https://example.test/with space", "\nhttps://example.test",
                      "https://example.test/\tsecret", "https://example.test/\x7f", "https://example.test/" + "a" * 2000,
                      "https://[broken", "", 123, {}):
            with self.subTest(value=value):
                self.assertIsNone(service._navigation_url(value))
                for field in ("shop_url", "shop_admin_url"):
                    with self.assertRaises(ValidationError):
                        EditorRowPatch(**patch_body(shops={"xtrek": {field: value}}))

    def test_description_is_bounded_text_with_explicit_blank_and_null_semantics(self):
        for value in ("Product description\nwith another line", "", None, "<p>Stored as text</p>"):
            item = EditorRowPatch(**patch_body(common={"description": value}))
            self.assertEqual(item.common.description, value)
        for value in ("a" * 20001, "bad\x00text", "bad\ud800text", 3):
            with self.assertRaises(ValidationError):
                EditorRowPatch(**patch_body(common={"description": value}))

    def test_manual_identity_preserves_source_strings_and_requires_supplier_scope(self):
        body = {"request_id": str(uuid4()), "name": "Simple local card", "supplier_id": 4,
            "supplier_sku": "000123", "eans": ["5901234123457"],
            "sku_identity": {"brand": "Topeak", "source_code": "000-UNIT", "code_kind": "manufacturer",
                "source_ref": "Manufacturer label", "leaf_confirmed": True}}
        item = ProductCardCreateRequest(**body)
        self.assertIsNone(item.sku)
        self.assertEqual(item.supplier_sku, "000123")
        self.assertEqual(item.sku_identity.source_code, "000-UNIT")
        for changed in ({"supplier_id": None}, {"supplier_sku": 123}, {"eans": ["12345"]},
                        {"sku_identity": {**body["sku_identity"], "source_code": 123}},
                        {"sku_identity": {**body["sku_identity"], "supplier_id": 8}}):
            with self.subTest(changed=changed), self.assertRaises(ValidationError):
                ProductCardCreateRequest(**{**body, **changed})

    def test_money_vat_and_minimum_are_explicit_decimal_strings_without_guessing(self):
        item = EditorRowPatch(**patch_body(variant={"sale_price_gross": "12.3", "vat_rate": None},
            warehouse={"min_quantity": "0"}, shops={"xtrek": {"sale_price_gross": "0", "visible": False}}))
        self.assertEqual(item.variant.sale_price_gross, "12.30")
        self.assertIsNone(item.variant.vat_rate)
        self.assertEqual(item.shops["xtrek"].sale_price_gross, "0.00")
        self.assertIs(item.shops["xtrek"].visible, False)
        for value in (12, 1.5, True, "-1", "01", "1.234", "1e2", "NaN", "Infinity", "1,20", " 12", "10000000000.00"):
            with self.subTest(price=value), self.assertRaises(ValidationError):
                EditorRowPatch(**patch_body(variant={"sale_price_gross": value}))
        for value in ("100.01", "-1", True, 23, "23.001"):
            with self.subTest(vat=value), self.assertRaises(ValidationError):
                EditorRowPatch(**patch_body(variant={"vat_rate": value}))
        for value in (1, True, "1.5", "1.0", "-1", "1e2"):
            with self.subTest(minimum=value), self.assertRaises(ValidationError):
                EditorRowPatch(**patch_body(warehouse={"min_quantity": value}))

    def test_read_only_identity_stock_cost_and_observed_shop_fields_are_not_editable(self):
        for field in ("sku", "ean", "group_id", "qty_on_hand", "qty_reserved", "avg_cost", "total_value", "shop_price"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                EditorRowPatch(**patch_body(**{field: "1"}))
            with self.subTest(scope="common", field=field), self.assertRaises(ValidationError):
                EditorRowPatch(**patch_body(common={field: "1"}))
        with self.assertRaises(ValidationError):
            EditorRowPatch(**patch_body(shops={"biketrek": {"stock": "1"}}))
        for value in (0, 1, "false", "true"):
            with self.assertRaises(ValidationError):
                EditorRowPatch(**patch_body(shops={"biketrek": {"visible": value}}))

    def test_confirmation_revision_and_text_fail_closed_without_losing_other_row_payloads(self):
        for value in (False, 1, "true", None):
            with self.assertRaises(ValidationError):
                ProductEditorSaveRequest(**save_body(confirmed=value))
        for value in (True, 1.0, "1", -1):
            with self.assertRaises(ValidationError):
                EditorRowPatch(**patch_body(expected_revision=value))
        for value in ("", "   ", "A" * 501, "bad\ud800name", "bad\x00name"):
            with self.assertRaises(ValidationError):
                EditorRowPatch(**patch_body(common={"name": value}))
        body = save_body(changes=[patch_body(), patch_body(product_id=2, variant={"sale_price_gross": "bad"})])
        parsed = ProductEditorSaveRequest(**body)
        self.assertEqual(len(parsed.changes), 2)  # Row-level validation happens atomically per item in the service.
        with self.assertRaises(ValidationError):
            ProductEditorSaveRequest(**save_body(changes=[patch_body()] * 101))


class ProductEditorOverrideTests(TestCase):
    def test_local_description_and_navigation_override_inherit_without_remapping_or_publication(self):
        source = facts()
        source["description"] = "Source description"
        source["shops"][0]["shop_url"] = "https://example.test/source-product"
        source["shops"][0]["shop_admin_url"] = "https://example.test/admin/42"
        item = EditorRowPatch(**patch_body(common={"description": ""},
            shops={"biketrek": {"shop_url": "https://example.test/new-link"}}))
        changed = service._apply_patch({}, item, None)
        row = service._make_row(source, changed, 1, None, {})
        self.assertEqual(row["common"]["description"], "")
        shop = row["shops"][0]
        self.assertEqual(shop["shop_url"], "https://example.test/new-link")
        self.assertEqual(shop["effective"]["shop_url"], shop["shop_url"])
        self.assertEqual(shop["shop_admin_url"], "https://example.test/admin/42")
        self.assertEqual(shop["mapping"], source["shops"][0]["mapping"])
        self.assertEqual(shop["state"], "inherited")  # Navigation is local, not a pending shop write.
        published = service.publication_values(row["common"], row["variant"], shop["effective"])
        self.assertNotIn("description", published)
        self.assertNotIn("shop_url", published)
        restored = service._apply_patch(changed, EditorRowPatch(**patch_body(common={"description": None},
            shops={"biketrek": {"shop_url": None}})), None)
        self.assertEqual(restored, {})
        inherited = service._make_row(source, restored, 2, None, {})
        self.assertEqual(inherited["common"]["description"], "Source description")
        self.assertEqual(inherited["shops"][0]["shop_url"], "https://example.test/source-product")

    def test_description_and_safe_navigation_sources_participate_in_snapshot_cas(self):
        source = facts()
        source["description"] = "Before"
        before = service._make_row(source, {}, 0, None, {})["snapshot_hash"]
        source["description"] = "After"
        self.assertNotEqual(before, service._make_row(source, {}, 0, None, {})["snapshot_hash"])
        source["description"] = "Before"
        source["shops"][0]["shop_url"] = "https://example.test/new-source-url"
        self.assertNotEqual(before, service._make_row(source, {}, 0, None, {})["snapshot_hash"])

    def test_shop_desired_price_never_inherits_unknown_basis_observed_price(self):
        row = service._make_row(facts(), {}, 0, None, {"known": False})
        self.assertIsNone(row["variant"]["sale_price_gross"])
        self.assertIsNone(row["variant"]["vat_rate"])
        self.assertEqual(row["common"]["name"], "Imported name")
        for shop in row["shops"]:
            self.assertIsNone(shop["effective"]["sale_price_gross"])
            self.assertEqual(shop["observed"]["price"], "24.00")
            self.assertEqual(shop["state"], "inherited")

    def test_partial_patch_preserves_other_scopes_and_false_or_zero_values(self):
        original = {"common": {"brand": "Manual brand"}, "variant": {"sale_price_gross": "9.99"},
                    "warehouses": {"north": {"location": "A1"}}, "shops": {"biketrek": {"name": "Shop name"}}}
        before = deepcopy(original)
        item = EditorRowPatch(**patch_body(common={"name": "New name"}, warehouse={"location": "B2", "min_quantity": "0"},
            shops={"xtrek": {"visible": False, "sale_price_gross": "0.00"}}))
        changed = service._apply_patch(original, item, "south")
        self.assertEqual(original, before)
        self.assertEqual(changed["warehouses"], {"north": {"location": "A1"}, "south": {"location": "B2", "min_quantity": "0"}})
        self.assertEqual(changed["shops"]["xtrek"], {"visible": False, "sale_price_gross": "0.00"})
        self.assertEqual(changed["shops"]["biketrek"], {"name": "Shop name"})
        self.assertEqual(changed["variant"], {"sale_price_gross": "9.99"})
        row = service._make_row(facts(), changed, 1, {"code": "south"}, {"known": False})
        self.assertEqual(row["shops"][0]["effective"]["name"], "Shop name")
        self.assertEqual(row["shops"][1]["effective"]["name"], "New name")
        self.assertEqual(row["shops"][1]["state"], "saved_unpublished")
        self.assertFalse(row["shops"][1]["effective"]["visible"])

    def test_explicit_null_resets_only_selected_overrides_to_inheritance(self):
        original = {"common": {"name": "Manual", "brand": "Brand"}, "shops": {"xtrek": {"visible": False}}}
        item = EditorRowPatch(**patch_body(common={"name": None}, shops={"xtrek": {"visible": None}}))
        changed = service._apply_patch(original, item, None)
        self.assertEqual(changed, {"common": {"brand": "Brand"}})
        row = service._make_row(facts(), changed, 2, None, {"known": False})
        self.assertEqual(row["common"]["name"], "Imported name")
        self.assertTrue(row["shops"][1]["effective"]["visible"])

    def test_source_snapshot_hash_ignores_live_stock_but_detects_imported_facts_and_warehouse_scope(self):
        base = service._make_row(facts(), {}, 0, {"code": "north"}, {"known": False})
        live = service._make_row(facts(), {"common": {"name": "Manual"}}, 3, {"code": "north"}, {"known": True})
        self.assertEqual(base["snapshot_hash"], live["snapshot_hash"])
        changed = facts()
        changed["name"] = "Another imported name"
        self.assertNotEqual(base["snapshot_hash"], service._make_row(changed, {}, 0, {"code": "north"}, {})["snapshot_hash"])
        self.assertNotEqual(base["snapshot_hash"], service._make_row(facts(), {}, 0, {"code": "south"}, {})["snapshot_hash"])
        self.assertNotIn("_facts", service._public(base))

    def test_images_are_bounded_safe_urls_and_never_fetch_remote_content(self):
        self.assertEqual(service._image("https://images.invalid/a.jpg"), "https://images.invalid/a.jpg")
        for value in ("javascript:alert(1)", "data:image/png;base64,PRIVATE", "http://images.invalid/a", "https://user:secret@images.invalid/a",
                      "https://images.invalid/" + "x" * 2000, None, {}):
            self.assertIsNone(service._image(value))


class ProductEditorQueryTests(IsolatedAsyncioTestCase):
    async def test_card_navigation_resolves_canonical_owner_without_changing_exact_writer_snapshot(self):
        db = object()
        with patch.object(service, "aliases_for", AsyncMock(return_value={7: 9})), \
             patch.object(service, "detail", AsyncMock(return_value={"id": 9})) as detail:
            self.assertEqual(await service.card_detail(db, 7, "central"), {"id": 9})
            detail.assert_awaited_once_with(db, 9, "central")

    async def test_exact_sku_claims_deduplicate_canonical_aliases_and_fail_on_ambiguity(self):
        db = SimpleNamespace(scalars=AsyncMock(return_value=[7, 9]))
        with patch.object(service, "aliases_for", AsyncMock(return_value={7: 9})), \
             patch.object(service, "detail", AsyncMock(return_value={"id": 9})) as detail:
            self.assertEqual(await service.detail_by_sku(db, "OLD-SKU", "central"), {"id": 9})
            detail.assert_awaited_once_with(db, 9, "central")
            sql = str(db.scalars.await_args.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
            self.assertIn("product_identifiers.identifier_type = 'internal_sku'", sql)
            self.assertNotIn("ILIKE", sql)
        for owners, code in (([], "product_editor_product_missing"), ([7, 9], "product_editor_product_ambiguous")):
            db.scalars.return_value = owners
            with patch.object(service, "aliases_for", AsyncMock(return_value={})), \
                 patch.object(service, "detail", AsyncMock()) as detail, self.assertRaises(service.EditorError) as raised:
                await service.detail_by_sku(db, "EXACT")
            self.assertEqual(raised.exception.code, code)
            detail.assert_not_awaited()

    async def test_bounded_listing_and_supplier_sources_compile_without_ambiguous_joins(self):
        class Rows:
            def all(self): return []
            def mappings(self): return self
            def __iter__(self): return iter(self.all())
        class DB:
            async def scalar(self, statement):
                statement.compile(dialect=postgresql.dialect())
                return 0
            async def scalars(self, statement):
                statement.compile(dialect=postgresql.dialect())
                return Rows()
            async def execute(self, statement, params=None):
                statement.compile(dialect=postgresql.dialect())
                return Rows()
        db = DB()
        result = await service.list_products(db, q="supplier-123", shop_code="biketrek", sort="group_sku")
        self.assertEqual(result["total"], 0)
        self.assertEqual(await service.effective(db, [1, 2]), {})


class ProductEditorProtectedRouteTests(TestCase):
    def setUp(self):
        self.db = SimpleNamespace(commit=AsyncMock(), execute=AsyncMock())
        self.sessions = []
        async def session():
            self.sessions.append(True)
            yield self.db
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[get_session] = session
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.token = "editor-test-only-operator-token-" + "x" * 24
        token = patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr(self.token))
        token.start()
        self.addCleanup(token.stop)
        self.headers = {"Authorization": "Bearer " + self.token}

    def requests(self):
        request_id = str(uuid4())
        return [("GET", "/product-editor/options", None, "options"),
                ("POST", "/product-editor/products", {"request_id": request_id, "sku": "LOCAL-CARD", "name": "Local card", "is_active": False}, "create_card"),
                ("GET", "/product-editor/products?warehouse_code=central&page_size=25&q=diel&sort=sku", None, "list_products"),
                ("GET", "/product-editor/products/resolve?sku=OLD-SKU&warehouse_code=central", None, "detail_by_sku"),
                ("GET", "/product-editor/products/12?warehouse_code=central", None, "card_detail"),
                ("POST", "/product-editor/save", save_body(request_id=request_id), "save"),
                ("GET", "/product-editor/saves/" + request_id, None, "get_save")]

    def operations(self, stack):
        return {name: stack.enter_context(patch.object(service, name, AsyncMock(return_value={"ok": name})))
                for name in ("options", "list_products", "card_detail", "detail_by_sku", "save", "get_save", "create_card")}

    def test_every_route_authenticates_before_database_business_or_upstream_calls(self):
        with ExitStack() as stack:
            methods = self.operations(stack)
            factory = stack.enter_context(patch.object(UpgatesClient, "from_shop"))
            for method, path, body, _ in self.requests():
                for headers in ({}, {"Authorization": "Bearer invalid"}):
                    response = self.client.request(method, path, json=body, headers=headers)
                    self.assertEqual(response.status_code, 401, response.text)
                    self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(self.sessions, [])
            for operation in methods.values():
                operation.assert_not_awaited()
            factory.assert_not_called()

    def test_authorized_endpoints_pass_exact_query_and_typed_save_without_caching(self):
        with ExitStack() as stack:
            methods = self.operations(stack)
            for method, path, body, name in self.requests():
                response = self.client.request(method, path, json=body, headers=self.headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), {"ok": name})
                self.assertEqual(response.headers["cache-control"], "no-store")
            methods["list_products"].assert_awaited_once_with(self.db, q="diel", brand=None, shop_code=None,
                warehouse_code="central", page=1, page_size=25, sort="sku", direction="asc", stock_scope="all", activity="all", supplier_link="all")
            methods["card_detail"].assert_awaited_once_with(self.db, 12, warehouse_code="central")
            methods["detail_by_sku"].assert_awaited_once_with(self.db, "OLD-SKU", warehouse_code="central")
            self.assertIsInstance(methods["save"].await_args.args[1], ProductEditorSaveRequest)

    def test_invalid_top_level_requests_never_echo_values_or_reach_business_operations(self):
        secret = "PRIVATE-INPUT"
        invalid = [("GET", "/product-editor/products?activity=" + secret, None),
                   ("GET", "/product-editor/products?supplier_link=" + secret, None),
                   ("POST", "/product-editor/products", {"request_id": str(uuid4()), "sku": "LOCAL", "name": "Card", "is_active": "false"}),
                   ("POST", "/product-editor/save", save_body(confirmed=1)),
                   ("POST", "/product-editor/save", save_body(extra=secret)),
                   ("POST", "/product-editor/save", save_body(request_id=secret)),
                   ("GET", "/product-editor/products?page_size=24", None),
                   ("GET", "/product-editor/products?page=0", None),
                   ("GET", "/product-editor/products?sort=" + secret, None),
                   ("GET", "/product-editor/products/resolve?sku=", None),
                   ("GET", "/product-editor/products/resolve?sku=" + "x" * 101, None),
                   ("GET", "/product-editor/saves/" + secret, None)]
        with ExitStack() as stack:
            methods = self.operations(stack)
            for method, path, body in invalid:
                response = self.client.request(method, path, content=json.dumps(body) if body is not None else None,
                    headers={**self.headers, "Content-Type": "application/json"})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json(), {"detail": {"code": "product_editor_invalid_request", "message": "product_editor_invalid_request"}})
                self.assertNotIn(secret, response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
            for operation in methods.values():
                operation.assert_not_awaited()

    def test_conflicts_and_missing_save_recovery_return_safe_no_store_errors(self):
        with patch.object(service, "save", AsyncMock(side_effect=service.EditorError("product_editor_request_reused"))):
            response = self.client.post("/product-editor/save", json=save_body(), headers=self.headers)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"]["code"], "product_editor_request_reused")
            self.assertEqual(response.headers["cache-control"], "no-store")
        with patch.object(service, "get_save", AsyncMock(side_effect=service.EditorError("product_editor_save_missing", 404))):
            response = self.client.get("/product-editor/saves/" + str(uuid4()), headers=self.headers)
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.headers["cache-control"], "no-store")
