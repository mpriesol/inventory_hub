import copy
import json
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from inventory_hub.ai_content_types import CategoryProfile, RuleBook, Rule
from inventory_hub.services import ai_content_existing as existing
from inventory_hub.services.catalog import CatalogError


def remote_product():
    return {"product_id": 17, "code": "REAL-SHOP-CODE", "ean": "12345", "manufacturer": "Example",
        "stock": 900, "prices": [{"price": "123.45"}], "buy_price": 41, "secret": "not-for-ai",
        "descriptions": [{"language": "sk", "title": "Pump model", "short_description": "Compact pump",
            "long_description": "<p>Aluminium body</p>", "seo_title": "SEO title", "seo_description": "Meta"}],
        "variants": [{"variant_id": 25, "code": "REAL-BLUE", "ean": "54321", "stock": 8}],
        "parameters": [{"descriptions": [{"language": "sk", "name": "Materiál"}],
            "values": [{"descriptions": [{"language": "sk", "value": "Hliník"}]}]}]}


class ExistingSourceTests(unittest.TestCase):
    def test_remote_ids_are_preserved_and_financial_data_excluded(self):
        remote = remote_product()
        snapshot = existing.source_snapshot(remote)
        self.assertEqual(snapshot["product_id"], 17)
        self.assertEqual(snapshot["variants"], [{"variant_id": 25, "code": "REAL-BLUE", "ean": "54321"}])
        serialized = json.dumps(snapshot)
        for forbidden in ("stock", "prices", "123.45", "buy_price", "not-for-ai"):
            self.assertNotIn(forbidden, serialized)
        products = existing.products_from_remote(remote, {"options": {"language": "sk"}})
        self.assertEqual(products[0].id, 17)
        self.assertEqual(products[0].shop_code, "REAL-SHOP-CODE")
        self.assertIsNone(products[0].supplier_stock)
        self.assertIsNone(products[0].prices.retail_gross)

    def test_stale_identity_and_content_block_but_stock_changes_do_not(self):
        original = remote_product()
        ctx = {"options": {"language": "sk"}, "source_digest": existing.service.digest(existing.source_snapshot(original))}
        changed = copy.deepcopy(original); changed["stock"] = 4
        existing.assert_source(changed, ctx)
        for mutate in (lambda r: r.update(product_id=18), lambda r: r["variants"][0].update(ean="changed"),
                       lambda r: r["descriptions"][0].update(long_description="Changed after AI began")):
            changed = copy.deepcopy(original); mutate(changed)
            with self.assertRaises(CatalogError) as error:
                existing.assert_source(changed, ctx)
            self.assertEqual(error.exception.code, "ai_existing_source_changed")
        confirmed = copy.deepcopy(original); confirmed["descriptions"][0]["title"] = "Confirmed AI update"
        ctx["update_source_digest"] = existing.service.digest(existing.source_snapshot(confirmed))
        existing.assert_source(confirmed, ctx)
        with self.assertRaises(CatalogError):
            existing.assert_source(original, ctx)

    def test_missing_real_id_or_language_refused(self):
        for field, value, code in (("product_id", None, "ai_existing_identity"), ("product_id", True, "ai_existing_identity"),
                                   ("descriptions", [{"language": "en", "title": "English only"}], "ai_existing_language")):
            product = remote_product(); product[field] = value
            with self.assertRaises(CatalogError) as error:
                existing.source_snapshot(product)
            self.assertEqual(error.exception.code, code)

    def test_thumbnail_does_not_expose_authenticated_urls(self):
        self.assertIsNone(existing.thumbnail({"images": [{"url": "https://user:secret@example.com/image.jpg"},
            {"url": "https://example.com/image.jpg?token=secret"}]}))
        self.assertEqual(existing.thumbnail({"images": [{"url": "https://example.com/image.jpg"}]}), "https://example.com/image.jpg")


class ExistingEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_entry_only_estimates_even_if_published_policy_disables_all_reviews(self):
        book = RuleBook(rules=[Rule(id="common", name="Common", policy={"review_required": False,
            "show_cost_estimate": False, "confirm_import": False})], categories=[CategoryProfile(id="general", name="General"),
                CategoryProfile(id="pump", name="Pumps", shop_categories={"test": "PUMPS"},
                    parameters=[{"name": "Farba", "scope": "variant", "required": False}])])
        db = AsyncMock(); db.get.return_value = None; db.add = lambda obj: None
        db.scalar.return_value = SimpleNamespace(platform="upgates")
        request = existing.ExistingProductRequest(request_id=uuid4(), shop="test", code="REAL-SHOP-CODE", research="feed_only")
        remote = remote_product(); remote["categories"] = [{"code": "PUMPS", "main_yn": True}]
        with patch.object(existing.rules, "published", AsyncMock(return_value=SimpleNamespace(id=2, book=book.model_dump()))), \
             patch.object(existing.imports, "shop_config", return_value={}), patch.object(existing.imports, "_target", return_value="target"), \
             patch.object(existing.UpgatesClient, "from_shop", return_value=object()), \
             patch("inventory_hub.services.ai_content_update.read_product", return_value=remote), \
             patch.object(existing.service.provider, "estimate", return_value=Decimal("0.2")), \
             patch.object(existing.service, "start", new_callable=AsyncMock) as start:
            result = await existing.create(db, request)
        self.assertEqual(result["status"], "estimate")
        self.assertEqual(result["product_ids"], [])
        self.assertEqual(result["facts"][0]["id"], 17)
        self.assertEqual(result["facts"][0]["source_kind"], "shop")
        self.assertEqual(result["category_profile"], "pump", "Existing main category selects its registered rules")
        self.assertTrue(result["policy"]["review_required"])
        self.assertTrue(result["policy"]["confirm_import"])
        self.assertTrue(result["policy"]["show_cost_estimate"])
        self.assertNotIn("not-for-ai", json.dumps(result, default=str))
        start.assert_not_called()

    async def test_reused_request_never_reads_shop_or_creates_new_job(self):
        request = existing.ExistingProductRequest(request_id=uuid4(), shop="test", code="REAL-SHOP-CODE")
        fingerprint = existing.service.digest({"source_kind": "shop", **request.model_dump(mode="json")})
        db = AsyncMock(); db.get.return_value = SimpleNamespace(request_hash=fingerprint)
        with patch.object(existing.service, "summary", return_value={"id": "existing"}), \
             patch.object(existing.UpgatesClient, "from_shop") as client:
            self.assertEqual(await existing.create(db, request), {"id": "existing"})
        client.assert_not_called()
