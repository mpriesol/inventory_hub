"""Identity configuration uses temporary files, never deployed supplier data."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from inventory_hub import config_io
from inventory_hub.config_normalize import normalize_supplier_config
from inventory_hub.routers import suppliers
from inventory_hub.supplier_prefix import (
    SupplierPrefixError, canonical_supplier_sku, get_supplier_prefix,
)
from inventory_hub.utils import save_supplier_config


def config(prefix):
    return {"name": "Synthetic supplier", "adapter_settings": {
        "mapping": {"postprocess": {"product_code_prefix": prefix}},
    }}


class SupplierPrefixTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for module in (config_io, suppliers):
            mock = patch.object(module, "DATA_ROOT", self.root)
            mock.start()
            self.addCleanup(mock.stop)

    def assert_error(self, code, action):
        with self.assertRaises(SupplierPrefixError) as error:
            action()
        self.assertEqual(code, error.exception.code)

    def test_new_prefix_editable_until_claim_and_metadata_cannot_unlock_it(self):
        config_io.save_supplier("synthetic", config("AA-"))
        self.assertFalse(config_io.supplier_prefix_status("synthetic")["product_prefix_locked"])
        config_io.save_supplier("synthetic", config("BB-"))
        config_io.claim_supplier_prefix("synthetic", "BB-")
        status = config_io.supplier_prefix_status("synthetic")
        self.assertTrue(status["product_prefix_locked"])
        self.assertEqual(status["product_prefix_lock_reason"], "used_for_product_identity")
        changed = {**config("CC-"), "product_prefix_locked": False, "product_prefix_lock_reason": None}
        self.assert_error("supplier_prefix_locked", lambda: config_io.save_supplier("synthetic", changed))
        self.assertEqual(get_supplier_prefix(config_io.load_supplier("synthetic")), "BB-")

    def test_existing_prefix_bootstraps_locked_even_before_first_new_save(self):
        path = config_io.supplier_path("synthetic")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(config("PL-")), encoding="utf-8")
        status = config_io.supplier_prefix_status("synthetic")
        self.assertEqual(status["product_prefix_lock_reason"], "legacy_existing_configuration")
        self.assert_error("supplier_prefix_locked", lambda: config_io.save_supplier("synthetic", config("OTHER-")))

    def test_all_legacy_alias_locations_are_enforced_and_conflicts_rejected(self):
        config_io.save_supplier("synthetic", config("PL-"))
        config_io.claim_supplier_prefix("synthetic", "PL-")
        for payload in ({"product_code_prefix": "OTHER-"}, {"adapter_settings": {"product_code_prefix": "OTHER-"}}, config("")):
            self.assert_error("supplier_prefix_locked", lambda: config_io.save_supplier("synthetic", payload))
        conflict = config("PL-")
        conflict["product_code_prefix"] = "OTHER-"
        self.assert_error("supplier_prefix_conflict", lambda: config_io.save_supplier("synthetic", conflict))
        self.assert_error("supplier_prefix_conflict", lambda: normalize_supplier_config(conflict))

    def test_alias_can_update_unused_prefix_without_being_shadowed(self):
        original = config("AA-")
        original["adapter_settings"]["mapping"]["invoice_to_canon"] = {"EAN": "supplier-ean"}
        config_io.save_supplier("synthetic", original)
        saved = config_io.save_supplier("synthetic", {"product_code_prefix": "BB-"})
        self.assertEqual(get_supplier_prefix(saved), "BB-")
        self.assertNotIn("product_code_prefix", saved)
        self.assertNotIn("product_code_prefix", saved["adapter_settings"])
        self.assertEqual(saved["adapter_settings"]["mapping"]["invoice_to_canon"], {"EAN": "supplier-ean"})

    def test_claim_checks_prepared_prefix_and_does_not_lock_newer_unseen_value(self):
        config_io.save_supplier("synthetic", config("AA-"))
        config_io.save_supplier("synthetic", config("BB-"))
        self.assert_error("supplier_prefix_changed", lambda: config_io.claim_supplier_prefix("synthetic", "AA-"))
        self.assertFalse(config_io.supplier_prefix_status("synthetic")["product_prefix_locked"])

    def test_prefix_unique_even_with_case_difference_and_when_inactive(self):
        config_io.save_supplier("first", {**config("PL-"), "is_active": False})
        self.assert_error("supplier_prefix_duplicate", lambda: config_io.save_supplier("second", config("pl-")))
        self.assertFalse(config_io.supplier_path("second").exists())

    def test_prefix_namespaces_cannot_overlap_in_either_direction(self):
        config_io.save_supplier("first", config("PL-A-"))
        for prefix in ("PL-", "pl-a-001-", "PL-A-"):
            self.assert_error("supplier_prefix_duplicate", lambda: config_io.save_supplier("second", config(prefix)))
        config_io.save_supplier("independent", config("PL-B-"))

    def test_concurrent_prefix_reservations_have_one_winner(self):
        barrier = Barrier(2)
        def save(code):
            barrier.wait(timeout=5)
            try:
                config_io.save_supplier(code, config("PL-"))
                return "saved"
            except SupplierPrefixError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, ("one", "two")))
        self.assertCountEqual(results, ["saved", "supplier_prefix_duplicate"])

    def test_existing_duplicate_prefixes_are_not_accepted_by_either_supplier(self):
        for code in ("one", "two"):
            path = config_io.supplier_path(code)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(config("PL-")), encoding="utf-8")
        for code in ("one", "two"):
            self.assert_error("supplier_prefix_duplicate", lambda: config_io.load_supplier(code))

    def test_invalid_supplier_is_isolated_but_its_valid_aliases_still_reserve_prefixes(self):
        bad = config("PL-")
        bad["product_code_prefix"] = "invalid prefix"
        path = config_io.supplier_path("broken")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(bad), encoding="utf-8")
        self.assert_error("supplier_prefix_invalid", lambda: config_io.load_supplier("broken"))
        self.assert_error("supplier_prefix_duplicate", lambda: config_io.save_supplier("collision", config("PL-")))
        config_io.save_supplier("independent", config("NF-"))
        self.assertEqual(get_supplier_prefix(config_io.load_supplier("independent")), "NF-")

    def test_prefix_remains_reserved_after_supplier_directory_removed(self):
        config_io.save_supplier("one", config("PL-"))
        config_io.claim_supplier_prefix("one", "PL-")
        config_io.supplier_path("one").unlink()
        self.assert_error("supplier_prefix_duplicate", lambda: config_io.save_supplier("two", config("PL-")))

    def test_restore_and_legacy_save_cannot_bypass_locked_prefix(self):
        config_io.save_supplier("synthetic", config("PL-"))
        config_io.claim_supplier_prefix("synthetic", "PL-")
        history = config_io.supplier_path("synthetic").parent / "config_history"
        history.mkdir()
        (history / "old.json").write_text(json.dumps(config("OLD-")), encoding="utf-8")
        with self.assertRaises(HTTPException) as error:
            suppliers.restore_supplier_version("synthetic", "old")
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(error.exception.detail["code"], "supplier_prefix_locked")
        self.assert_error("supplier_prefix_locked", lambda: save_supplier_config("synthetic", config("OLD-")))

    def test_metadata_is_http_only_and_claim_preserves_config_fingerprint(self):
        original = config("PL-")
        before = copy.deepcopy(original)
        config_io.save_supplier("synthetic", original)
        self.assertEqual(original, before)
        serialized = config_io.supplier_path("synthetic").read_bytes()
        config_io.claim_supplier_prefix("synthetic", "PL-")
        self.assertEqual(config_io.supplier_path("synthetic").read_bytes(), serialized)
        returned = suppliers.get_supplier_config("synthetic")
        self.assertTrue(returned["product_prefix_locked"])
        suppliers.put_supplier_config("synthetic", returned)
        self.assertEqual(config_io.supplier_path("synthetic").read_bytes(), serialized)
        self.assertNotIn("product_prefix_locked", config_io.load_supplier("synthetic"))

    def test_replaced_mapping_keeps_only_the_protected_omitted_prefix(self):
        original = config("PL-")
        original["adapter_settings"]["mapping"]["invoice_to_canon"] = {"EAN": "ean"}
        config_io.save_supplier("synthetic", original)
        config_io.claim_supplier_prefix("synthetic", "PL-")
        loaded = config_io.save_supplier("synthetic", {"adapter_settings": {"mapping": {"postprocess": {"custom": True}}}})
        self.assertEqual(get_supplier_prefix(loaded), "PL-")
        self.assertNotIn("invoice_to_canon", loaded["adapter_settings"]["mapping"])
        self.assertTrue(loaded["adapter_settings"]["mapping"]["postprocess"]["custom"])
        self.assert_error("supplier_prefix_invalid", lambda: config_io.save_supplier("synthetic", {"adapter_settings": {"mapping": None}}))

    def test_config_round_trip_can_clear_headers_remove_feed_sources_and_mapping_fields(self):
        original = config("PL-")
        original["feeds"] = {"sources": {
            "products": {"remote": {"headers": {"X-Test": "old-value"}, "params": {"obsolete": "value"}}},
            "obsolete": {"mode": "remote"},
        }}
        original["adapter_settings"]["mapping"]["invoice_to_canon"] = {"EAN": "old-ean", "TITLE": "title"}
        config_io.save_supplier("synthetic", original)
        config_io.claim_supplier_prefix("synthetic", "PL-")
        changed = config_io.load_supplier("synthetic")
        remote = changed["feeds"]["sources"]["products"]["remote"]
        remote["headers"] = {}
        del remote["params"]["obsolete"]
        del changed["feeds"]["sources"]["obsolete"]
        del changed["adapter_settings"]["mapping"]["invoice_to_canon"]["EAN"]
        suppliers.put_supplier_config("synthetic", changed)
        loaded = config_io.load_supplier("synthetic")
        self.assertEqual(loaded["feeds"]["sources"]["products"]["remote"], {"headers": {}, "params": {}})
        self.assertNotIn("obsolete", loaded["feeds"]["sources"])
        self.assertEqual(loaded["adapter_settings"]["mapping"]["invoice_to_canon"], {"TITLE": "title"})
        self.assertEqual(get_supplier_prefix(loaded), "PL-")

    def test_corrupt_supplier_file_is_not_normalized_enrolled_or_overwritten(self):
        path = config_io.supplier_path("broken")
        path.parent.mkdir(parents=True)
        for contents in (b'{"adapter_settings":', b"[1, 2]", b"null", b"", b"\xff"):
            with self.subTest(contents=contents):
                path.write_bytes(contents)
                for action in (
                    lambda: config_io.load_supplier("broken", write_back_on_load=False),
                    lambda: config_io.load_supplier("broken", write_back_on_load=True),
                    lambda: config_io.save_supplier("broken", config("PL-")),
                    lambda: config_io.claim_supplier_prefix("broken", "PL-"),
                ):
                    self.assert_error("supplier_config_invalid", action)
                    self.assertEqual(path.read_bytes(), contents)
                registry = self.root / "suppliers" / ".product-prefixes.json"
                self.assertFalse(registry.exists())

    def test_unknown_corrupt_supplier_allows_unrelated_reads_but_blocks_new_reservations(self):
        config_io.save_supplier("healthy", config("NF-"))
        path = config_io.supplier_path("broken")
        path.parent.mkdir(parents=True)
        path.write_text("{broken", encoding="utf-8")
        self.assertEqual(get_supplier_prefix(config_io.load_supplier("healthy")), "NF-")
        config_io.save_supplier("healthy", {"name": "Updated unrelated setting"})
        for action in (
            lambda: config_io.save_supplier("new", config("PL-")),
            lambda: config_io.save_supplier("healthy", config("CHANGED-")),
            lambda: config_io.claim_supplier_prefix("healthy", "NF-"),
        ):
            self.assert_error("supplier_prefix_validation_incomplete", action)
        self.assertFalse(config_io.supplier_path("new").exists())

    def test_corrupt_supplier_with_immutable_reservation_does_not_block_known_other_namespace(self):
        config_io.save_supplier("broken", config("PL-"))
        config_io.claim_supplier_prefix("broken", "PL-")
        path = config_io.supplier_path("broken")
        path.write_text("{broken", encoding="utf-8")
        config_io.save_supplier("healthy", config("NF-"))
        config_io.claim_supplier_prefix("healthy", "NF-")
        self.assert_error("supplier_prefix_duplicate", lambda: config_io.save_supplier("collision", config("PL-")))
        self.assert_error("supplier_config_invalid", lambda: config_io.load_supplier("broken"))
        self.assertEqual(path.read_text(), "{broken")

    def test_registry_corruption_fails_closed(self):
        config_io.save_supplier("synthetic", config("PL-"))
        (self.root / "suppliers" / ".product-prefixes.json").write_text("broken", encoding="utf-8")
        self.assert_error("supplier_prefix_registry_invalid", lambda: config_io.save_supplier("synthetic", config("OTHER-")))

    def test_zeroes_and_supplier_code_characters_are_preserved(self):
        self.assertEqual(canonical_supplier_sku("PL-", "00123-a/b"), "PL-00123-a/b")
        for code in (123, "", " 00123", "a" * 98, "001\x7f23"):
            self.assert_error("supplier_code_invalid", lambda: canonical_supplier_sku("PL-", code))

    def test_invalid_prefix_and_empty_first_use_are_rejected(self):
        for prefix in (123, "PL -", "PL/", "a" * 21):
            self.assert_error("supplier_prefix_invalid", lambda: config_io.save_supplier("synthetic", config(prefix)))
        config_io.save_supplier("synthetic", config(""))
        self.assert_error("supplier_prefix_required", lambda: config_io.claim_supplier_prefix("synthetic", ""))
        with self.assertRaises(HTTPException) as error:
            suppliers.create_supplier(suppliers.SupplierCreateRequest(code="new", name="New"))
        self.assertEqual(error.exception.detail["code"], "supplier_prefix_required")
