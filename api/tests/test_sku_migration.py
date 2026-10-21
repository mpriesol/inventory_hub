"""Bounded authenticated read-only review and exact remote identity checks."""
from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from inventory_hub.database import get_session
from inventory_hub.routers import sku_policy as routes
from inventory_hub.settings import settings
from inventory_hub.sku_migration_types import SkuMigrationPreviewRequest
from inventory_hub.services import sku_migration as service


def body(product_id=1, **changes):
    return {"items": [{"product_id": product_id, "sku_identity": {"brand": "Topeak", "source_code": "LEAF-01",
        "code_kind": "manufacturer", "source_ref": "Fixture manufacturer specification", "leaf_confirmed": True}}], **changes}


def mapping(**changes):
    return {"old_code": "OLD-01", "proposed_code": "TP-LEAF-01", "is_variant": True,
        "native_id": "101", "parent_code": "GROUP", "parent_native_id": "800", "reasons": [], **changes}


def remote(**changes):
    return {"code": "OLD-01", "is_variant": True, "external_id": "101", "parent_code": "GROUP",
        "parent_id": "800", "eans": ["727670929720"], "attributes": [{"name": "Color", "value": "Black"}], **changes}


class SkuMigrationInputTests(TestCase):
    def test_small_unique_batches_and_no_apply_or_stock_fields(self):
        parsed = SkuMigrationPreviewRequest(**body())
        self.assertFalse(parsed.verify_remote)
        invalid = [body(items=[]), body(items=body()["items"] * 2), body(verify_remote=1),
                   body(apply=True), body(stock_enabled=True),
                   body(items=[{**body()["items"][0], "product_id": i} for i in range(1, 22)])]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                SkuMigrationPreviewRequest(**value)

    def test_remote_gtin_equivalence_and_exact_native_parent_identity(self):
        item = {"identity": {"barcodes": ["0727670929720"]}, "_attributes": [{"name": "Farba", "value": "Čierna"}]}
        current = mapping()
        service._verify_mapping(current, item, {"leaves": [remote()], "occupied_codes": []})
        self.assertEqual(current["remote_status"], "verified")
        self.assertEqual(current["parent_native_id"], "800")
        self.assertEqual(len(current["remote_hash"]), 64)
        for change, reason in (({"external_id": "102"}, "migration_remote_native_id_changed"),
            ({"parent_id": "801"}, "migration_remote_parent_changed"),
            ({"eans": ["5901234123457"]}, "migration_remote_barcode_conflict"),
            ({"eans": []}, "migration_remote_barcode_missing"),
            ({"attributes": [{"name": "Color", "value": "Green"}]}, "migration_remote_variant_conflict")):
            current = mapping()
            service._verify_mapping(current, item, {"leaves": [remote(**change)], "occupied_codes": []})
            self.assertIn(reason, current["reasons"])
            self.assertEqual(current["remote_status"], "conflict")

    def test_target_parent_or_case_variant_is_an_occupied_code(self):
        item = {"identity": {"barcodes": []}, "_attributes": []}
        current = mapping()
        service._verify_mapping(current, item, {"leaves": [remote()], "occupied_codes": [
            {"code": "tp-leaf-01", "native_id": "900", "kind": "parent"}]})
        self.assertIn("migration_remote_target_occupied", current["reasons"])
        current = mapping()
        service._verify_mapping(current, item, {"leaves": [remote()]})
        self.assertIn("migration_remote_target_collision_unverified", current["reasons"])


class SkuMigrationRouteTests(TestCase):
    def setUp(self):
        self.db = SimpleNamespace(commit=AsyncMock(), flush=AsyncMock())
        async def session():
            yield self.db
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[get_session] = session
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.token = "test-migration-token-" + "x" * 32
        auth = patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr(self.token))
        auth.start()
        self.addCleanup(auth.stop)
        self.headers = {"Authorization": "Bearer " + self.token}

    def test_preview_requires_operator_and_never_commits(self):
        denied = self.client.post("/sku-policy/migration-preview", json=body())
        self.assertIn(denied.status_code, (401, 403))
        with patch.object(service, "preview", AsyncMock(return_value={"can_apply": False, "items": []})) as preview:
            result = self.client.post("/sku-policy/migration-preview", headers=self.headers, json=body())
        self.assertEqual(result.status_code, 200)
        self.assertIs(result.json()["can_apply"], False)
        self.assertEqual(result.headers["cache-control"], "no-store")
        preview.assert_awaited_once()
        self.db.commit.assert_not_awaited()
        self.db.flush.assert_not_awaited()
        self.assertEqual(self.client.post("/sku-policy/migration-apply", headers=self.headers, json=body()).status_code, 404)
