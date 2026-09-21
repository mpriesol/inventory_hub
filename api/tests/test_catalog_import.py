import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from catalog_fixtures import FakeUpgates, dummy_session, product
from inventory_hub import config_io
from inventory_hub.catalog_types import CatalogParameter, ShopImportOptions
from inventory_hub.services import catalog_import as imports


class ImportMappingTests(unittest.TestCase):
    def test_gross_and_net_prices_reuse_coefficients_and_omit_stock(self):
        cfg = {"adapter_settings": {"price_coefficients": {"TEST": "0.9"}}}
        p = product()
        for with_vat, sale, purchase in [(True, 110.7, 73.8), (False, 90, 60)]:
            item = imports.build_item([p], ShopImportOptions(), cfg, with_vat)
            self.assertEqual(item.status, "ready")
            price = item.payload["prices"][0]
            self.assertEqual(price["pricelists"][0]["price_original"], sale)
            self.assertEqual(price["price_purchase"], purchase)
            self.assertFalse(item.payload["active_yn"])
            self.assertEqual(item.payload["metas"], [{"key": "validation_required", "value": "1"}])
            imports._assert_payload(item.payload)
        with self.assertRaises(imports.CatalogError):
            imports._assert_payload({**item.payload, "variants": [{"stocks": []}]})

    def test_only_selected_variants_are_exported_with_their_images(self):
        p = product(group_code="G1", variant_relationship="explicit", variant_attributes=[CatalogParameter(name="Size", value="M")])
        v = product(2, group_code="G1", variant_relationship="explicit", variant_attributes=[CatalogParameter(name="Size", value="L")])
        item = imports.build_item([v], ShopImportOptions(), {}, True)
        self.assertEqual(item.code, "PL-G-G1")
        self.assertEqual(item.product_ids, [2])
        self.assertEqual([v["code"] for v in item.payload["variants"]], ["PL-A-002"])
        self.assertEqual(item.payload["variants"][0]["image"]["url"], v.images[0])
        self.assertFalse(item.payload["active_yn"])
        v.variant_attributes = p.variant_attributes
        self.assertEqual(imports.build_item([p, v], ShopImportOptions(), {}, True).status, "invalid")

    def test_price_currency_and_vat_mismatch_block_import(self):
        p = product()
        p.prices.retail_net = None
        self.assertIn("missing_price", imports.build_item([p], ShopImportOptions(), {}, False).errors)
        p = product()
        p.prices.retail_gross = 119
        self.assertIn("invalid_price_vat", imports.build_item([p], ShopImportOptions(), {}, True).errors)
        self.assertIn("currency_mismatch", imports.build_item([product()], ShopImportOptions(currency="CZK"), {}, True).errors)


class ImportExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.object(config_io, "DATA_ROOT", self.root).start()
        cfg_path = config_io.shop_path("test-shop")
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text(json.dumps({"upgates_api_base_url": "https://test.example.com/api/v2", "upgates_login": "test-fixture", "upgates_api_key": "test-fixture"}))
        self.client = FakeUpgates()
        patch.object(imports.UpgatesClient, "from_shop", return_value=self.client).start()
        patch.object(imports, "get_session_context", dummy_session).start()
        patch.object(imports, "selected_products", AsyncMock(return_value=[product()])).start()
        patch.object(imports, "local_identities", AsyncMock(return_value=({}, set()))).start()
        self.register = patch.object(imports, "register_created", AsyncMock()).start()
        self.id = "a" * 32
        self.path = imports._path("test-shop", self.id)
        p = product()
        item = imports.build_item([p], ShopImportOptions(), {}, True)
        self.document = {"target": imports._target(imports.shop_config("test-shop")), "prices_with_vat": True,
            "sources": [p.model_dump(mode="json")], "result": None,
            "preview": {"preview_id": self.id, "shop": "test-shop", "supplier": "paul-lange", "errors": [],
                "expires_at": (imports.now() + timedelta(hours=1)).isoformat(), "options": ShopImportOptions().model_dump(),
                "create_validation_field": False, "items": [item.model_dump(mode="json")]}}
        imports._write(self.path, self.document)

    async def run_import(self):
        result, queued = imports.queue_import("test-shop", self.id)
        if queued:
            await imports.execute_import("test-shop", self.id)
        return imports.import_result("test-shop", self.id)

    async def test_success_is_verified_and_repeated_confirmation_is_idempotent(self):
        result = await self.run_import()
        self.assertEqual(result["items"][0]["status"], "created")
        self.register.assert_awaited_once()
        self.assertEqual(len(self.client.sent), 1)
        await self.run_import()
        self.assertEqual(len(self.client.sent), 1)

    async def test_timeout_after_creation_is_reconciled_without_resending(self):
        self.client.timeout = True
        result = await self.run_import()
        self.assertEqual(result["items"][0]["status"], "created")
        self.assertIn("import_verified_by_readback", result["items"][0]["warnings"])
        self.assertEqual(len(self.client.sent), 1)

    async def test_unknown_outcome_never_blindly_retries_post(self):
        self.client.timeout, self.client.accept = True, False
        result = await self.run_import()
        self.assertEqual(result["items"][0]["status"], "uncertain")
        _, queued = imports.queue_import("test-shop", self.id, retry_failed=True)
        self.assertTrue(queued)
        await imports.execute_import("test-shop", self.id)
        self.assertEqual(len(self.client.sent), 1)
        self.register.assert_not_awaited()

    async def test_existing_ean_under_another_code_is_not_sent(self):
        self.client.products["OTHER"] = {"code": "OTHER", "ean": product().eans[0]}
        result = await self.run_import()
        self.assertEqual(result["items"][0]["status"], "exists")
        self.assertEqual(self.client.sent, [])

    async def test_database_failure_after_send_can_be_recovered_without_post(self):
        self.register.side_effect = RuntimeError("simulated DB failure")
        result = await self.run_import()
        self.assertEqual(result["items"][0]["status"], "uncertain")
        self.register.side_effect = None
        imports.queue_import("test-shop", self.id, retry_failed=True)
        await imports.execute_import("test-shop", self.id)
        self.assertEqual(imports.import_result("test-shop", self.id)["items"][0]["status"], "created")
        self.assertEqual(len(self.client.sent), 1)

    async def test_target_price_mode_changed_blocks_all_writes(self):
        self.client.with_vat = False
        result = await self.run_import()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["errors"], ["shop_options_changed"])
        self.assertEqual(self.client.sent, [])

    async def test_process_restart_recovers_durable_item_intent(self):
        imports.queue_import("test-shop", self.id)
        document = imports._load_job(self.path)
        document["result"]["status"] = "running"
        imports._save_job(self.path, document)
        item = document["result"]["items"][0]
        item["status"] = "uncertain"
        imports._item_checkpoint(self.path, document["result"], item)
        self.client.post("products", {"products": [item["payload"]]})
        # Simulate termination before the full job document was saved.
        with patch.object(imports, "now", return_value=imports.now() + timedelta(minutes=1)):
            interrupted = imports.import_result("test-shop", self.id)
            self.assertEqual(interrupted["status"], "failed")
            self.assertEqual(interrupted["items"][0]["status"], "uncertain")
            self.assertNotIn("descriptions", interrupted["items"][0]["payload"])
            imports.queue_import("test-shop", self.id, retry_failed=True)
            await imports.execute_import("test-shop", self.id)
            self.assertEqual(imports.import_result("test-shop", self.id)["items"][0]["status"], "created")
        self.assertEqual(len(self.client.sent), 1)

    async def test_incomplete_variant_response_is_not_reported_as_success(self):
        p = product(group_code="G1", variant_relationship="explicit", variant_attributes=[CatalogParameter(name="Size", value="M")])
        v = product(2, group_code="G1", variant_relationship="explicit", variant_attributes=[CatalogParameter(name="Size", value="L")])
        item = imports.build_item([p, v], ShopImportOptions(), {}, True)
        self.document["sources"] = [source.model_dump(mode="json") for source in (p, v)]
        self.document["preview"]["items"] = [item.model_dump(mode="json")]
        imports._write(self.path, self.document)
        self.client.omit_variant = True
        with patch.object(imports, "selected_products", AsyncMock(return_value=[p, v])):
            result = await self.run_import()
        self.assertEqual(result["items"][0]["status"], "uncertain")
        self.assertEqual(result["items"][0]["errors"], ["import_readback_mismatch"])
        self.register.assert_not_awaited()

    def test_queued_double_click_and_shop_lock_do_not_duplicate_jobs(self):
        _, first = imports.queue_import("test-shop", self.id)
        _, second = imports.queue_import("test-shop", self.id)
        self.assertTrue(first)
        self.assertFalse(second)
        with imports._shop_lock("test-shop"):
            with self.assertRaises(imports.CatalogError):
                with imports._shop_lock("test-shop"):
                    self.fail("Second shop lock must be rejected")

    def test_expired_preview_cannot_be_confirmed(self):
        self.document["preview"]["expires_at"] = (imports.now() - timedelta(seconds=1)).isoformat()
        imports._write(self.path, self.document)
        with self.assertRaises(imports.CatalogError) as raised:
            imports.queue_import("test-shop", self.id)
        self.assertEqual(raised.exception.code, "preview_expired")
