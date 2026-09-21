"""API call savings and fresh duplicate checks against synthetic shop responses."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from catalog_fixtures import FakeUpgates
from inventory_hub import config_io
from inventory_hub.services import catalog_import as imports
from inventory_hub.services.upgates import UpgatesError


class ShopCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        p = patch.object(config_io, "DATA_ROOT", self.root)
        p.start(); self.addCleanup(p.stop)
        self.current = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        p = patch.object(imports, "now", side_effect=lambda: self.current)
        p.start(); self.addCleanup(p.stop)
        self.cfg = {"upgates_api_base_url": "https://shop.example.test/api/v2", "upgates_login": "fixture", "upgates_api_key": "fixture-secret"}
        for shop in ("one", "two"):
            path = config_io.shop_path(shop)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(self.cfg))
        self.client = FakeUpgates()
        self.client.get = Mock(wraps=self.client.get)

    def test_options_cache_saves_calls_and_exposes_age_expiry_and_refresh(self):
        fresh = imports.cached_import_options("one", self.client)
        initial = self.client.get.call_count
        self.assertEqual(initial, 5)
        self.assertFalse(fresh["cache"]["from_cache"])
        self.current += timedelta(minutes=2)
        cached = imports.cached_import_options("one", self.client)
        self.assertEqual(self.client.get.call_count, initial)
        self.assertTrue(cached["cache"]["from_cache"])
        self.assertEqual(cached["cache"]["checked_at"], fresh["cache"]["checked_at"])
        imports.cached_import_options("one", self.client, refresh=True)
        self.assertEqual(self.client.get.call_count, initial * 2)
        self.current += timedelta(minutes=16)
        imports.cached_import_options("one", self.client)
        self.assertEqual(self.client.get.call_count, initial * 3)

    def test_cache_is_scoped_to_shop_and_connection_and_never_falls_back_on_failure(self):
        imports.cached_import_options("one", self.client)
        imports.cached_import_options("two", self.client)
        self.assertEqual(self.client.get.call_count, 10)
        config_io.shop_path("one").write_text(json.dumps({**self.cfg, "upgates_api_key": "changed-fixture-secret"}))
        imports.cached_import_options("one", self.client)
        self.assertEqual(self.client.get.call_count, 15)
        self.client.get.side_effect = UpgatesError("synthetic failure")
        with self.assertRaises(imports.CatalogError):
            imports.cached_import_options("one", self.client, refresh=True)
        content = (config_io.shop_path("one").parent / "catalog-cache/options.json").read_text()
        self.assertNotIn("fixture-secret", content)

    def test_concurrent_check_does_not_duplicate_requests(self):
        with imports._shop_cache("one", "options"):
            with self.assertRaises(imports.CatalogError) as error:
                imports.cached_import_options("one", self.client)
        self.assertEqual(error.exception.code, "shop_check_running")
        self.client.get.assert_not_called()

    def test_incremental_check_preserves_unchanged_items_and_replaces_changed_variants(self):
        first = {"product_id": 1, "code": "PARENT", "variants": [{"code": "V1", "ean": "0001"}]}
        second = {"product_id": 2, "code": "UNCHANGED", "ean": "0002"}
        self.client.get.side_effect = [
            {"products": [first], "number_of_pages": 2}, {"products": [second], "number_of_pages": 2},
            {"products": [{"product_id": 1, "code": "RENAMED", "variants": [{"code": "V2", "ean": "0003"}]}], "number_of_pages": 1}]
        codes, eans, initial = imports.checked_remote_identities("one", self.client)
        self.assertEqual(eans, {"0001", "0002"})
        self.current += timedelta(minutes=1)
        codes, eans, changed = imports.checked_remote_identities("one", self.client)
        self.assertEqual(codes, {"renamed", "v2", "unchanged"})
        self.assertEqual(eans, {"0002", "0003"})
        self.assertEqual(changed["mode"], "changes")
        self.assertEqual(changed["full_checked_at"], initial["full_checked_at"])
        params = self.client.get.call_args.args[1]
        self.assertEqual(datetime.fromisoformat(params["last_update_time_from"]), datetime.fromisoformat(initial["checked_at"]) - timedelta(minutes=5))
        self.assertEqual(self.client.get.call_count, 3, "Full scan takes two pages; the next check fetches one changed page")

    def test_full_check_removes_deleted_products_and_failed_pages_do_not_advance_checkpoint(self):
        self.client.products = {"OLD": {"product_id": 1, "code": "OLD", "ean": "0001"}}
        imports.checked_remote_identities("one", self.client)
        path = config_io.shop_path("one").parent / "catalog-cache/identities.json"
        original = path.read_bytes()
        self.client.get.side_effect = [{"products": [{"product_id": 2, "code": "NEW"}], "number_of_pages": 2}, UpgatesError("page two failed")]
        with self.assertRaises(imports.CatalogError):
            imports.checked_remote_identities("one", self.client)
        self.assertEqual(path.read_bytes(), original)
        self.client.get.side_effect = None
        self.client.products = {}
        codes, _, status = imports.checked_remote_identities("one", self.client, refresh=True)
        self.assertEqual(codes, set())
        self.assertEqual(status["mode"], "full")
        self.current += timedelta(hours=25)
        _, _, status = imports.checked_remote_identities("one", self.client)
        self.assertEqual(status["mode"], "full")

