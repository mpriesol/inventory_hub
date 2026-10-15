"""Bounded identities and authenticated link-only API; no real shop or database."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from inventory_hub.database import get_session
from inventory_hub.db_models import IdentifierType
from inventory_hub.product_link_types import (
    ProductLinkMatch, ProductLinksApplyRequest, ProductLinksCandidatesRequest, ProductLinksPreviewRequest,
    ProductLinksRefreshRequest,
)
from inventory_hub.routers import product_links as routes
from inventory_hub.services import product_links as service
from inventory_hub.services.product_identity import IdentityIndex
from inventory_hub.settings import settings


def preview_body(**changes):
    return {"request_id": str(uuid4()), "shop_code": "xtrek", "product_ids": [1, 2], **changes}


def apply_body(**changes):
    return {"preview_hash": "a" * 64, "product_ids": [1], "confirmed": True, **changes}


class ProductLinksInputTests(unittest.TestCase):
    def test_selection_is_bounded_distinct_and_uses_strict_database_ids(self):
        for ids in ([], [1, 1], [0], [-1], [True], ["1"], [1.0], list(range(1, 102))):
            with self.subTest(ids=ids), self.assertRaises(ValidationError):
                ProductLinksPreviewRequest(**preview_body(product_ids=ids))
            with self.subTest(scope="apply", ids=ids), self.assertRaises(ValidationError):
                ProductLinksApplyRequest(**apply_body(product_ids=ids))
            with self.subTest(scope="refresh", ids=ids), self.assertRaises(ValidationError):
                ProductLinksRefreshRequest(shop_code="xtrek", product_ids=ids)
        self.assertEqual(ProductLinksCandidatesRequest(shop_code="xtrek", product_ids=[1], query="exact-code").query,
                         "exact-code")
        with self.assertRaises(ValidationError):
            ProductLinksPreviewRequest(**preview_body(query="unreviewed search"))

    def test_refresh_cannot_choose_new_identity_or_mutate_prices_or_stock(self):
        for extra in ({"manual_matches": []}, {"external_id": "999"}, {"query": "OTHER"},
                      {"shop_price": "1.00"}, {"shop_stock": 100}, {"create_missing": True}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                ProductLinksRefreshRequest(shop_code="xtrek", product_ids=[1], **extra)

    def test_manual_match_is_exact_and_cannot_inject_price_stock_or_new_product(self):
        valid = {"product_id": 1, "code": "0001-M", "parent_code": "0001", "is_variant": True}
        match = ProductLinkMatch(**valid)
        self.assertEqual((match.code, match.parent_code), ("0001-M", "0001"))
        for code in ("", " leading", "trailing ", "A;B", "line\ncode", "bad\x00code", "bad\ud800code", "x" * 101):
            with self.subTest(code=code), self.assertRaises(ValidationError):
                ProductLinkMatch(**{**valid, "code": code})
        for extra in ({"price": "9.99"}, {"qty_on_hand": "1"}, {"create_missing": True}, {"external_id": "injected"}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                ProductLinkMatch(**{**valid, **extra})
        for changes in ({"product_id": 3}, {"is_variant": "true"}, {"is_variant": False}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                ProductLinksPreviewRequest(**preview_body(manual_matches=[{**valid, **changes}]))
        with self.assertRaises(ValidationError):
            ProductLinksPreviewRequest(**preview_body(manual_matches=[valid, valid]))

    def test_apply_requires_literal_confirmation_and_valid_frozen_hash(self):
        for value in (False, 1, "true", None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ProductLinksApplyRequest(**apply_body(confirmed=value))
        for value in ("", "a" * 63, "A" * 64, "g" * 64):
            with self.subTest(hash=value), self.assertRaises(ValidationError):
                ProductLinksApplyRequest(**apply_body(preview_hash=value))
        with self.assertRaises(ValidationError):
            ProductLinksApplyRequest(**apply_body(shop_code="biketrek"))

    def test_frozen_selection_expires_but_completed_exact_replay_remains_recoverable(self):
        at = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
        document = {"rows": [{"product_id": 1, "status": "ready"}, {"product_id": 2, "status": "not_found"}]}
        row = SimpleNamespace(document=document, preview_hash=service.digest(document), status="prepared",
                              expires_at=at + timedelta(minutes=30), result=None)
        payload = ProductLinksApplyRequest(**apply_body(preview_hash=row.preview_hash))
        with patch.object(service, "now", return_value=at):
            self.assertEqual(service._apply_selection(row, payload), [document["rows"][0]])
        with patch.object(service, "now", return_value=row.expires_at), self.assertRaises(service.LinkError) as expired:
            service._apply_selection(row, payload)
        self.assertEqual(expired.exception.code, "product_links_preview_expired")
        row.status, row.result = "completed", {"product_ids": [1]}
        with patch.object(service, "now", return_value=at + timedelta(days=2)):
            self.assertIsNone(service._apply_selection(row, payload))
        document["rows"][0]["product_id"] = 3
        with self.assertRaises(service.LinkError) as changed:
            service._apply_selection(row, payload)
        self.assertEqual(changed.exception.code, "product_links_preview_changed")


class ProductLinksIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.facts = {"product_id": 1, "sku": "LOCAL-CODE", "name": "Existing Hub title", "is_active": True,
                      "eans": ["5901234123457"], "attributes": [], "manual_eans": None}
        self.remote = {"code": "REMOTE-CODE", "parent_code": "REMOTE-CODE", "is_variant": False,
                       "external_id": "101", "parent_id": "101", "name": "Existing remote title",
                       "eans": ["5901234123457"], "attributes": []}

    async def rows(self, facts, choices, *, mappings=(), extra_products=(), extra_identifiers=()):
        products = [SimpleNamespace(id=item["product_id"], sku=item["sku"], is_active=item["is_active"])
                    for item in facts.values()]
        identifiers = [SimpleNamespace(product_id=item["product_id"], value=ean,
            identifier_type=IdentifierType.ean, supplier_id=None) for item in facts.values() for ean in item["eans"]]
        index = IdentityIndex(2, [*products, *extra_products], mappings, [*identifiers, *extra_identifiers])
        with patch.object(service, "load_identity_index", AsyncMock(return_value=index)):
            return await service._rows(SimpleNamespace(), SimpleNamespace(id=2), facts, choices)

    async def test_different_codes_require_actual_barcode_or_explicit_manual_choice(self):
        choice = service._choice(self.facts, [self.remote])
        self.assertEqual(choice[1:], ("validated_barcode", 1))
        rows = await self.rows({1: self.facts}, {1: choice})
        self.assertEqual((rows[0]["status"], rows[0]["match_by"]), ("ready", "validated_barcode"))
        remote = {**self.remote, "eans": [], "name": self.facts["name"]}
        self.assertIsNone(service._choice(self.facts, [remote])[0], "Matching titles are not identity evidence")
        manual = {key: remote[key] for key in ("code", "parent_code", "is_variant")}
        choice = service._choice(self.facts, [remote], manual)
        rows = await self.rows({1: self.facts}, {1: choice})
        self.assertEqual(rows[0]["status"], "ready")
        self.assertIn("barcode_missing", rows[0]["warnings"])

    async def test_raw_invalid_barcode_evidence_is_visible_without_becoming_an_identity_match(self):
        remote = {**self.remote, "eans": [], "raw_barcodes": ["5901234123458/legacy-note"],
                  "invalid_barcodes": ["5901234123458", "legacy-note"], "warnings": ["unverified_barcode"]}
        self.assertIsNone(service._choice(self.facts, [remote])[0])
        manual = {key: remote[key] for key in ("code", "parent_code", "is_variant")}
        rows = await self.rows({1: self.facts}, {1: service._choice(self.facts, [remote], manual)})
        self.assertEqual(rows[0]["status"], "ready")
        self.assertEqual(rows[0]["remote"]["raw_barcodes"], ["5901234123458/legacy-note"])
        self.assertEqual(rows[0]["remote"]["eans"], [])
        self.assertIn("unverified_barcode", rows[0]["warnings"])
        self.assertIn("barcode_missing", rows[0]["warnings"])

    async def test_cached_second_barcode_owner_blocks_automatic_choice_even_if_only_one_is_fresh(self):
        other = {**self.remote, "code": "SECOND-CODE", "parent_code": "SECOND-CODE",
                 "external_id": "202", "parent_id": "202"}
        choice = service._choice(self.facts, [self.remote], cached=[self.remote, other])
        self.assertEqual(choice, (None, "validated_barcode", 2))
        rows = await self.rows({1: self.facts}, {1: choice})
        self.assertEqual(rows[0]["status"], "conflict")
        self.assertIn("ambiguous_remote_match", rows[0]["errors"])
        manual = {key: self.remote[key] for key in ("code", "parent_code", "is_variant")}
        self.assertEqual(service._choice(self.facts, [self.remote], manual=manual, cached=[self.remote, other]),
                         (self.remote, "manual", 1))

    async def test_unsupported_local_code_returns_validation_error_before_remote_request(self):
        db = SimpleNamespace(get=AsyncMock(return_value=None), commit=AsyncMock())
        with patch.object(service, "_shop", AsyncMock(return_value=SimpleNamespace(id=2))), \
             patch.object(service, "_facts", AsyncMock(return_value={1: {**self.facts, "sku": "A;B"}})), \
             patch.object(service, "_cached", AsyncMock(return_value=([], False))), \
             patch.object(service.source, "discover", AsyncMock()) as discover:
            with self.assertRaises(service.LinkError) as error:
                await service.preview(db, ProductLinksPreviewRequest(**preview_body(product_ids=[1])))
            self.assertEqual((error.exception.code, error.exception.status), ("product_links_unsupported_code", 422))
            discover.assert_not_awaited()
            db.commit.assert_not_awaited()

    async def test_manual_matching_does_not_override_foreign_sku_or_barcode_owner(self):
        other = SimpleNamespace(id=2, sku="REMOTE-CODE", is_active=True)
        rows = await self.rows({1: self.facts}, {1: (self.remote, "manual", 1)}, extra_products=[other])
        self.assertEqual(rows[0]["status"], "conflict")
        self.assertIn("assigned_product_changed", rows[0]["errors"])
        remote = {**self.remote, "eans": ["4006381333931"]}
        identifier = SimpleNamespace(product_id=2, value="4006381333931", identifier_type=IdentifierType.ean, supplier_id=None)
        rows = await self.rows({1: self.facts}, {1: (remote, "manual", 1)},
            extra_products=[SimpleNamespace(id=2, sku="UNRELATED", is_active=True)], extra_identifiers=[identifier])
        self.assertEqual(rows[0]["status"], "conflict")

    async def test_cleared_editor_ean_is_unknown_but_foreign_barcode_owner_still_blocks(self):
        facts = {**self.facts, "eans": [], "manual_eans": []}
        owner = SimpleNamespace(id=2, sku="OTHER-PRODUCT", is_active=True)
        identifier = SimpleNamespace(product_id=2, value=self.remote["eans"][0],
                                     identifier_type=IdentifierType.ean, supplier_id=None)
        for mode, code in (("shared_sku", facts["sku"]), ("manual", self.remote["code"])):
            with self.subTest(mode=mode):
                remote = {**self.remote, "code": code, "parent_code": code}
                manual = {key: remote[key] for key in ("code", "parent_code", "is_variant")} if mode == "manual" else None
                choice = service._choice(facts, [remote], manual=manual)
                self.assertEqual(choice[1:], (mode, 1))
                rows = await self.rows({1: facts}, {1: choice})
                self.assertEqual(rows[0]["status"], "ready")
                self.assertIn("barcode_missing", rows[0]["warnings"])
                self.assertNotIn("manual_barcode_conflict", rows[0]["errors"])
                rows = await self.rows({1: facts}, {1: choice},
                                       extra_products=[owner], extra_identifiers=[identifier])
                self.assertEqual(rows[0]["status"], "conflict")
                self.assertIn("assigned_product_changed", rows[0]["errors"])

    async def test_translated_variant_axes_agree_but_size_or_manual_ean_conflict_blocks(self):
        facts = {**self.facts, "attributes": [{"name": "Farba", "value": "Čierna"}, {"name": "Veľkosť", "value": "M"}]}
        remote = {**self.remote, "is_variant": True, "parent_code": "FAMILY", "parent_id": "800",
            "attributes": [{"name": "Color", "value": "Black"}, {"name": "Size", "value": "M"}]}
        rows = await self.rows({1: facts}, {1: (remote, "manual", 1)})
        self.assertEqual(rows[0]["status"], "ready")
        self.assertNotIn("variant_axes_missing", rows[0]["warnings"])
        remote["attributes"][1]["value"] = "L"
        rows = await self.rows({1: facts}, {1: (remote, "manual", 1)})
        self.assertIn("variant_axes_conflict", rows[0]["errors"])
        rows = await self.rows({1: {**self.facts, "manual_eans": ["4006381333931"]}},
                               {1: (self.remote, "manual", 1)})
        self.assertIn("manual_barcode_conflict", rows[0]["errors"])

    async def test_two_cards_cannot_select_same_remote_leaf(self):
        other = {**self.facts, "product_id": 2, "sku": "OTHER-LOCAL", "eans": []}
        rows = await self.rows({1: self.facts, 2: other}, {1: (self.remote, "manual", 1), 2: (self.remote, "manual", 1)})
        self.assertTrue(all(row["status"] == "conflict" for row in rows))
        self.assertTrue(all("remote_target_selected_twice" in row["errors"] for row in rows))

    async def test_existing_variant_parent_or_listing_state_cannot_be_silently_replaced(self):
        remote = {**self.remote, "parent_code": "NEW-PARENT", "is_variant": True}
        mapping = SimpleNamespace(id=7, product_id=1, shop_id=2, external_id="101", external_code="REMOTE-CODE",
            variant_code="REMOTE-CODE", parent_code="OLD-PARENT", is_variant=True, is_listed=True)
        rows = await self.rows({1: self.facts}, {1: (remote, "manual", 1)}, mappings=[mapping])
        self.assertEqual(rows[0]["status"], "conflict")
        self.assertIn("existing_mapping_differs", rows[0]["errors"])
        mapping.parent_code, mapping.is_listed = "NEW-PARENT", False
        rows = await self.rows({1: self.facts}, {1: (remote, "manual", 1)}, mappings=[mapping])
        self.assertEqual(rows[0]["status"], "conflict")


class ProductLinksFailureFenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        at = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
        document = {"rows": [{"product_id": 1, "status": "ready"}, {"product_id": 2, "status": "not_found"}]}
        self.row = SimpleNamespace(document=document, preview_hash=service.digest(document), status="prepared",
                                   expires_at=at + timedelta(days=1), result=None)
        self.payload = ProductLinksApplyRequest(**apply_body(preview_hash=self.row.preview_hash))
        self.db = SimpleNamespace(get=AsyncMock(return_value=self.row), rollback=AsyncMock(),
            execute=AsyncMock(), scalar=AsyncMock(return_value=self.row), commit=AsyncMock())

    async def test_bad_hash_or_subset_never_expires_another_valid_operation(self):
        for changes in ({"preview_hash": "b" * 64}, {"product_ids": [2]}, {"product_ids": [3]}):
            payload = self.payload.model_copy(update=changes)
            with self.subTest(changes=changes), patch.object(service, "_apply_prepared", AsyncMock()) as prepared, \
                 patch.object(service, "_fence_failed_apply", AsyncMock()) as fence:
                with self.assertRaises(service.LinkError) as error:
                    await service.apply(self.db, str(uuid4()), payload)
                self.assertFalse(getattr(error.exception, "known_unapplied", False))
                prepared.assert_not_awaited()
                fence.assert_not_awaited()
        self.db.commit.assert_not_awaited()

    async def test_unknown_apply_outcome_never_claims_durable_nonapplication(self):
        with patch.object(service, "_apply_prepared", AsyncMock(side_effect=TimeoutError("simulated uncertain commit"))), \
             patch.object(service, "_fence_failed_apply", AsyncMock()) as fence:
            with self.assertRaises(TimeoutError):
                await service.apply(self.db, str(uuid4()), self.payload)
            fence.assert_not_awaited()

    async def test_failed_fence_commit_cannot_mark_error_known_unapplied(self):
        error = service.LinkError("product_links_remote_changed")
        self.db.commit.side_effect = RuntimeError("simulated fence commit failure")
        with self.assertRaises(RuntimeError):
            await service._fence_failed_apply(self.db, str(uuid4()), self.payload, error)
        self.assertFalse(getattr(error, "known_unapplied", False))
        self.db.rollback.assert_awaited_once()
        self.db.commit.assert_awaited_once()

    async def test_known_unapplied_marker_is_set_only_after_fence_commit_returns(self):
        error = service.LinkError("product_links_remote_changed")
        async def commit():
            self.assertFalse(getattr(error, "known_unapplied", False))
            self.assertLessEqual(self.row.expires_at, service.now())
        self.db.commit.side_effect = commit
        with self.assertRaises(service.LinkError) as raised:
            await service._fence_failed_apply(self.db, str(uuid4()), self.payload, error)
        self.assertIs(raised.exception, error)
        self.assertIs(error.known_unapplied, True)
        self.db.commit.assert_awaited_once()


class ProductLinksRouteTests(unittest.TestCase):
    def setUp(self):
        self.db_calls = []
        async def session():
            self.db_calls.append(True)
            yield SimpleNamespace()
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[get_session] = session
        self.app = app
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        token = "synthetic-product-links-operator-" + "x" * 24
        patcher = patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr(token))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.headers = {"Authorization": "Bearer " + token}
        identifier = str(uuid4())
        self.requests = [
            ("POST", "/product-links/candidates", {"shop_code": "xtrek", "product_ids": [1]}, "candidates"),
            ("POST", "/product-links/preview", preview_body(), "preview"),
            ("POST", "/product-links/refresh", {"shop_code": "xtrek", "product_ids": [1]}, "refresh"),
            ("GET", "/product-links/" + identifier, None, "get"),
            ("POST", "/product-links/" + identifier + "/apply", apply_body(), "apply"),
        ]

    def test_operator_auth_precedes_database_and_remote_access_for_every_route(self):
        with ExitStack() as stack:
            operations = [stack.enter_context(patch.object(service, name, AsyncMock())) for *_, name in self.requests]
            for method, path, body, _ in self.requests:
                response = self.client.request(method, path, json=body)
                self.assertEqual(response.status_code, 401, response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(self.db_calls, [])
            for operation in operations:
                operation.assert_not_awaited()

    def test_authenticated_routes_delegate_once_without_caching(self):
        for method, path, body, name in self.requests:
            with self.subTest(path=path), patch.object(service, name, AsyncMock(return_value={"ok": True})) as operation:
                response = self.client.request(method, path, json=body, headers=self.headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
                operation.assert_awaited_once()
                if name in ("preview", "apply"):
                    self.assertEqual(operation.await_args.kwargs["actor"], {"source": "operator_token"})

    def test_cookie_session_writes_require_same_origin_before_database_or_service(self):
        @self.app.middleware("http")
        async def cookie_principal(request, call_next):
            request.state.hub_user = {"id": 7, "role": "operator"}
            request.state.hub_bearer = False
            return await call_next(request)

        rejected = [
            {},
            {"Origin": "http://testserver"},
            {"Origin": "https://foreign.example", "X-Hub-Request": "1"},
            {"Origin": "http://testserver", "X-Hub-Request": "1", "Sec-Fetch-Site": "cross-site"},
        ]
        for method, path, body, name in self.requests:
            if method != "POST":
                continue
            with self.subTest(path=path), patch.object(service, name, AsyncMock(return_value={"ok": True})) as operation:
                calls_before = len(self.db_calls)
                for headers in rejected:
                    response = self.client.post(path, json=body, headers=headers)
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertEqual(response.json()["detail"]["code"], "hub_origin_rejected")
                    self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(len(self.db_calls), calls_before)
                operation.assert_not_awaited()
                response = self.client.post(path, json=body, headers={"Origin": "http://testserver",
                    "X-Hub-Request": "1", "Sec-Fetch-Site": "same-origin"})
                self.assertEqual(response.status_code, 200, response.text)
                operation.assert_awaited_once()
                if name in ("preview", "apply"):
                    self.assertEqual(operation.await_args.kwargs["actor"], {"user_id": 7})

    def test_invalid_payload_and_service_errors_do_not_echo_sensitive_input(self):
        response = self.client.post("/product-links/preview", headers=self.headers,
            json=preview_body(unexpected_secret="do-not-echo-this-value"))
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("do-not-echo-this-value", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        with patch.object(service, "preview", AsyncMock(side_effect=service.LinkError("product_links_changed"))):
            response = self.client.post("/product-links/preview", headers=self.headers, json=preview_body())
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"]["code"], "product_links_changed")
            self.assertEqual(response.headers["cache-control"], "no-store")

    def test_apply_error_exposes_known_unapplied_only_with_durable_service_marker(self):
        path = "/product-links/" + str(uuid4()) + "/apply"
        for known in (False, True):
            error = service.LinkError("product_links_remote_changed")
            if known:
                error.known_unapplied = True
            with self.subTest(known=known), patch.object(service, "apply", AsyncMock(side_effect=error)):
                response = self.client.post(path, headers=self.headers, json=apply_body())
                self.assertEqual(response.status_code, 409)
                detail = response.json()["detail"]
                self.assertEqual(detail["code"], "product_links_remote_changed")
                if known:
                    self.assertIs(detail.get("known_unapplied"), True)
                else:
                    self.assertNotIn("known_unapplied", detail)
