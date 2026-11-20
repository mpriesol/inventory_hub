"""Lossless policy input, protected registry APIs and migration packaging."""
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from inventory_hub.database import get_session
from inventory_hub.routers import sku_policy as routes
from inventory_hub.settings import settings
from inventory_hub.sku_policy_types import BrandCreate, BrandSupplier, SkuIdentityInput
from inventory_hub.services import sku_policy as service


def evidence(**changes):
    return {"brand": "Topeak", "source_code": "0001410", "code_kind": "manufacturer",
            "source_ref": "Manufacturer leaf specification, test fixture", "leaf_confirmed": True, **changes}


class SkuPolicyInputTests(TestCase):
    def test_codes_are_strict_original_text_and_not_parent_or_counter_fields(self):
        value = SkuIdentityInput(**evidence())
        self.assertEqual(value.source_code, "0001410")
        for field, invalid in (("source_code", 1410), ("source_code", " 0001410"),
                               ("source_code", "0001410 "), ("source_code", "bad;code"),
                               ("source_code", "bad\x00code"), ("source_code", "bad\ud800code"),
                               ("leaf_confirmed", 1), ("source_ref", ""), ("code_kind", "parent")):
            with self.subTest(field=field, value=invalid), self.assertRaises(ValidationError):
                SkuIdentityInput(**evidence(**{field: invalid}))
        with self.assertRaises(ValidationError):
            SkuIdentityInput(**evidence(next_number=1))

    def test_only_brand_spelling_normalizes_and_supplier_identity_is_scoped(self):
        self.assertEqual(service.brand_key("  ToPEAK  "), service.brand_key("Topeak"))
        self.assertNotEqual(service.brand_key("Race Face"), service.brand_key("RaceFace"))
        first = SkuIdentityInput(**evidence())
        self.assertNotEqual(service.identity_key(1, first),
                            service.identity_key(1, SkuIdentityInput(**evidence(source_code="1410"))))
        self.assertNotEqual(service.identity_key(1, SkuIdentityInput(**evidence(code_kind="supplier", supplier_id=1))),
                            service.identity_key(1, SkuIdentityInput(**evidence(code_kind="supplier", supplier_id=2))))
        self.assertEqual(service.identity_key(1, SkuIdentityInput(**evidence(code_kind="gtin", source_code="727670929720"))),
                         service.identity_key(1, SkuIdentityInput(**evidence(code_kind="gtin", source_code="0727670929720"))))
        for code in ("tp", "T", "TOP", "ŤP", "T1"):
            with self.subTest(code=code), self.assertRaises(ValidationError):
                BrandCreate(name="Test", code=code, source_ref="Test registry")

    def test_b2b_links_accept_only_public_portal_addresses_and_unique_supplier_names(self):
        portal = BrandSupplier(name="  Sample   Trade ", url="https://b2b.example.org/account/")
        self.assertEqual(portal.name, "Sample Trade")
        self.assertEqual(BrandSupplier(name="Manual").url, "")
        for query in ("act=noredirect", "route=account/login", "lang=sk&category=123"):
            url = f"https://b2b.example.org/?{query}"
            self.assertEqual(BrandSupplier(name="Example", url=url).url, url)
        for invalid in ("example.org", "javascript:alert(1)", "https://user:pass@example.org",
                        "https://example.org/catalog?key=value", "https://example.org/#session",
                        "https://example.org/?%61ccess_token=x", "https://example.org/?PHPSESSID=x",
                        "https://example.org/?X-Amz-Signature=x", "https://example.org/?next=/account",
                        "https://example.org/?r=https%3A%2F%2Fexample.org%2F%3Ftoken%3Dx",
                        "https://example.org/?r=%252F%252Fevil.example",
                        "https://example.org:123456/", "https://bad host.com/", "https://example.org/\nnext"):
            with self.subTest(url=invalid), self.assertRaises(ValidationError):
                BrandSupplier(name="Sample", url=invalid)
        with self.assertRaises(ValidationError):
            BrandCreate(name="Maker", code="MK", source_ref="Verified catalog",
                        suppliers=[portal, BrandSupplier(name="SAMPLE    Trade")])

    def test_upgrade_is_packaged_and_ordered_without_product_or_stock_backfill(self):
        root = Path(__file__).resolve().parents[2]
        sql = (root / "infra/db-init/029_sku_policy.sql").read_text()
        docker = (root / "api/Dockerfile").read_text()
        self.assertIn("COPY infra/db-init/029_sku_policy.sql", docker)
        for mutation in ("UPDATE products", "INSERT INTO products", "stock_movements", "stock_balances"):
            self.assertNotIn(mutation, sql)


class SkuPolicyRouteTests(TestCase):
    def setUp(self):
        self.db = SimpleNamespace(commit=AsyncMock())
        async def session():
            yield self.db
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[get_session] = session
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.token = "test-sku-policy-token-" + "x" * 32
        auth = patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr(self.token))
        auth.start()
        self.addCleanup(auth.stop)
        self.headers = {"Authorization": "Bearer " + self.token}

    def test_unauthenticated_registry_and_preview_are_closed(self):
        for path, body in (("/sku-policy/brands", {"name": "Test", "code": "TE", "source_ref": "Test"}),
                           ("/sku-policy/preview", {"sku_identity": evidence()}),
                           ("/sku-policy/brands/1/aliases", {"alias": "Test", "source_ref": "Test"})):
            with self.subTest(path=path):
                self.assertIn(self.client.post(path, json=body).status_code, (401, 403))
        self.assertIn(self.client.get("/sku-policy/brands").status_code, (401, 403))
        self.db.commit.assert_not_awaited()

    def test_preview_is_read_only_and_can_lookup_existing_without_allocation_proof(self):
        with patch.object(service, "preview_sku", AsyncMock(return_value={"status": "existing", "product_id": 42})) as preview:
            response = self.client.post("/sku-policy/preview", headers=self.headers,
                                        json={"barcodes": ["727670929720"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIsNone(preview.call_args.args[1])
        self.db.commit.assert_not_awaited()

    def test_registry_collision_is_visible_and_never_committed(self):
        with patch.object(service, "create_brand", AsyncMock(side_effect=service.SkuPolicyError("sku_brand_code_taken"))):
            response = self.client.post("/sku-policy/brands", headers=self.headers,
                json={"name": "Test", "code": "TP", "source_ref": "Test fixture"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "sku_brand_code_taken")
        self.db.commit.assert_not_awaited()
