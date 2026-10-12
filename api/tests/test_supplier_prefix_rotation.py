"""Explicit supplier namespace rotation uses temporary configuration only."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from pydantic import SecretStr

from inventory_hub import config_io
from inventory_hub.routers import supplier_prefixes
from inventory_hub.settings import settings
from inventory_hub.supplier_prefix import SupplierPrefixError, get_supplier_prefix
from test_supplier_prefix import config


class SupplierPrefixRotationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = patch.object(config_io, "DATA_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        original = config("SL-")
        original["adapter_settings"]["price_coefficients"] = {"Synthetic brand": 1}
        original["adapter_settings"]["mapping"]["invoice_to_canon"] = {"EAN": "supplier-ean"}
        original["feeds"] = {"sources": {"products": {"mode": "local", "local_path": "synthetic.xml"}}}
        config_io.save_supplier("sloger", original)
        config_io.claim_supplier_prefix("sloger", "SL-")
        self.path = config_io.supplier_path("sloger")
        self.registry_path = self.root / "suppliers" / ".product-prefixes.json"

    def registry(self):
        return json.loads(self.registry_path.read_text())

    def rotate(self, old="SL-", new="SLG-"):
        return config_io.rotate_supplier_prefix("sloger", old, new)

    def assert_error(self, code, action):
        with self.assertRaises(SupplierPrefixError) as error:
            action()
        self.assertEqual(error.exception.code, code)
        self.assertEqual(error.exception.status, 409)

    def test_explicit_rotation_preserves_unrelated_config_and_keeps_both_namespaces(self):
        before = config_io.load_supplier("sloger")
        result = self.rotate()
        self.assertEqual(result["supplier"], "sloger")
        self.assertEqual(result["previous_prefix"], "SL-")
        self.assertEqual(result["product_prefix"], "SLG-")
        self.assertEqual(result["retired_prefixes"], ["SL-"])
        self.assertFalse(result["replayed"])
        after = config_io.load_supplier("sloger")
        self.assertEqual(get_supplier_prefix(after), "SLG-")
        after["adapter_settings"]["mapping"]["postprocess"]["product_code_prefix"] = "SL-"
        self.assertEqual(after, before, "Rotation must preserve feeds, mappings, prices and other settings")
        status = config_io.supplier_prefix_status("sloger")
        self.assertTrue(status["product_prefix_locked"])
        entry = self.registry()["suppliers"]["sloger"]
        self.assertEqual(entry["prefix"], "SLG-")
        self.assertEqual(entry["retired_prefixes"], ["SL-"])
        self.assertTrue(entry["locked"])

    def test_replay_is_idempotent_and_stale_expected_value_cannot_rotate_again(self):
        self.rotate()
        saved = (self.path.read_bytes(), self.registry_path.read_bytes())
        replay = self.rotate()
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["product_prefix"], "SLG-")
        self.assertEqual(replay["retired_prefixes"], ["SL-"])
        self.assertEqual((self.path.read_bytes(), self.registry_path.read_bytes()), saved)
        for old, new in (("UNSEEN-", "SLG-"), ("SL-", "NEXT-")):
            with self.subTest(old=old, new=new):
                self.assert_error("supplier_prefix_changed", lambda: self.rotate(old, new))
                self.assertEqual((self.path.read_bytes(), self.registry_path.read_bytes()), saved)

    def test_ordinary_save_cannot_rotate_a_locked_current_prefix_or_restore_the_old_one(self):
        self.assert_error("supplier_prefix_locked", lambda: config_io.save_supplier("sloger", config("SLG-")))
        self.rotate()
        for prefix in ("SL-", "OTHER-"):
            with self.subTest(prefix=prefix):
                self.assert_error("supplier_prefix_locked", lambda: config_io.save_supplier("sloger", config(prefix)))
        self.assertEqual(get_supplier_prefix(config_io.load_supplier("sloger")), "SLG-")
        self.assertEqual(config_io.claim_supplier_prefix("sloger", "SLG-"), "SLG-")
        self.assertEqual(self.registry()["suppliers"]["sloger"]["retired_prefixes"], ["SL-"])

    def test_current_and_retired_other_supplier_namespaces_block_rotation(self):
        config_io.save_supplier("other", config("NF-"))
        config_io.claim_supplier_prefix("other", "NF-")
        config_io.rotate_supplier_prefix("other", "NF-", "NFG-")
        for prefix in ("nf-", "NF-CHILD-", "N", "nfg-", "NFG-CHILD-"):
            with self.subTest(prefix=prefix):
                before = (self.path.read_bytes(), self.registry_path.read_bytes())
                self.assert_error("supplier_prefix_duplicate", lambda: self.rotate(new=prefix))
                self.assertEqual((self.path.read_bytes(), self.registry_path.read_bytes()), before)

    def test_retired_namespace_stays_reserved_even_without_supplier_configuration(self):
        self.rotate()
        self.path.unlink()
        for prefix in ("sl-", "SL-CHILD-", "slg-", "S"):
            with self.subTest(prefix=prefix):
                self.assert_error("supplier_prefix_duplicate", lambda: config_io.save_supplier("new", config(prefix)))
                self.assertFalse(config_io.supplier_path("new").exists())

    def test_rotation_cannot_overlap_own_current_or_retired_namespace(self):
        # SL- + A-123 and SL-A- + 123 must never become the same canonical SKU.
        for prefix in ("SL-A-", "S", "sl-"):
            with self.subTest(prefix=prefix):
                self.assert_error("supplier_prefix_rotation_overlap", lambda: self.rotate(new=prefix))
        self.rotate()
        before = (self.path.read_bytes(), self.registry_path.read_bytes())
        self.assert_error("supplier_prefix_rotation_overlap", lambda: self.rotate("SLG-", "SL-A-"))
        self.assertEqual((self.path.read_bytes(), self.registry_path.read_bytes()), before)

    def test_claim_and_ordinary_save_preserve_retirement_history_and_extra_registry_fields(self):
        registry = self.registry()
        registry["suppliers"]["sloger"]["future_metadata"] = {"evidence": ["preserve-me"]}
        self.registry_path.write_text(json.dumps(registry))
        self.rotate()
        expected = copy.deepcopy(self.registry()["suppliers"]["sloger"])
        self.assertEqual(expected["future_metadata"], {"evidence": ["preserve-me"]})
        config_io.claim_supplier_prefix("sloger", "SLG-")
        config_io.save_supplier("sloger", {"name": "Updated supplier name"})
        actual = self.registry()["suppliers"]["sloger"]
        self.assertEqual(actual["future_metadata"], expected["future_metadata"])
        self.assertEqual(actual["retired_prefixes"], ["SL-"])
        self.assertEqual(actual["prefix"], "SLG-")
        self.assertTrue(actual["locked"])
        self.assertEqual(config_io.load_supplier("sloger")["name"], "Updated supplier name")

    def test_later_rotation_accumulates_history_and_cannot_reactivate_retired_prefix(self):
        self.rotate()
        result = self.rotate("SLG-", "SLH-")
        self.assertCountEqual(result["retired_prefixes"], ["SL-", "SLG-"])
        self.assertFalse(result["replayed"])
        before = (self.path.read_bytes(), self.registry_path.read_bytes())
        for prefix in ("SL-", "SLG-"):
            with self.subTest(prefix=prefix):
                self.assert_error("supplier_prefix_retired", lambda: self.rotate("SLH-", prefix))
                self.assertEqual((self.path.read_bytes(), self.registry_path.read_bytes()), before)

    def test_empty_invalid_and_unchanged_prefixes_leave_configuration_untouched(self):
        before = (self.path.read_bytes(), self.registry_path.read_bytes())
        for old, new in (("", "SLG-"), ("SL-", ""), ("SL-", "BAD PREFIX"), ("SL-", "A" * 21), ("SL-", "SL-")):
            with self.subTest(old=old, new=new):
                with self.assertRaises(SupplierPrefixError) as error:
                    self.rotate(old, new)
                if new == old:
                    self.assertEqual(error.exception.code, "supplier_prefix_rotation_unchanged")
                self.assertEqual((self.path.read_bytes(), self.registry_path.read_bytes()), before)

    def test_concurrent_rotations_from_same_expected_prefix_have_one_winner(self):
        barrier = Barrier(2)

        def rotate(new):
            barrier.wait(timeout=5)
            try:
                return self.rotate(new=new)
            except SupplierPrefixError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(rotate, ("SLG-", "SLN-")))
        accepted = [result for result in results if isinstance(result, dict)]
        rejected = [result for result in results if isinstance(result, str)]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(rejected, ["supplier_prefix_changed"])
        self.assertEqual(get_supplier_prefix(config_io.load_supplier("sloger")), accepted[0]["product_prefix"])
        self.assertEqual(self.registry()["suppliers"]["sloger"]["retired_prefixes"], ["SL-"])

    def test_failure_after_registry_commit_blocks_all_supplier_identity_operations(self):
        original_write = config_io._atomic_write
        original_config = self.path.read_bytes()

        def fail_config_write(path, data):
            if path == self.path:
                raise OSError("Synthetic configuration write failure")
            original_write(path, data)

        with patch.object(config_io, "_atomic_write", side_effect=fail_config_write):
            with self.assertRaises(OSError):
                self.rotate()
        self.assertEqual(self.path.read_bytes(), original_config)
        entry = self.registry()["suppliers"]["sloger"]
        self.assertEqual(entry["prefix"], "SLG-", "New and old namespaces must be reserved before config changes")
        self.assertEqual(entry["retired_prefixes"], ["SL-"])
        for action in (
            lambda: config_io.load_supplier("sloger"),
            lambda: config_io.save_supplier("sloger", {"name": "Must not silently reconcile"}),
            lambda: config_io.claim_supplier_prefix("sloger", "SL-"),
            lambda: self.rotate(),
        ):
            self.assert_error("supplier_prefix_locked", action)
            self.assertEqual(self.path.read_bytes(), original_config)

    def test_failure_before_registry_commit_never_changes_supplier_configuration(self):
        original_write = config_io._atomic_write
        before = (self.path.read_bytes(), self.registry_path.read_bytes())

        def fail_registry_write(path, data):
            if path == self.registry_path:
                raise OSError("Synthetic registry write failure")
            original_write(path, data)

        with patch.object(config_io, "_atomic_write", side_effect=fail_registry_write):
            with self.assertRaises(OSError):
                self.rotate()
        self.assertEqual((self.path.read_bytes(), self.registry_path.read_bytes()), before)
        self.assertEqual(get_supplier_prefix(config_io.load_supplier("sloger")), "SL-")


class SupplierPrefixRotationRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_rotation_requires_operator_access_and_returns_uncached_validated_results(self):
        app = FastAPI()
        app.include_router(supplier_prefixes.router)
        token = "synthetic-operator-token-123456789012345"
        payload = {"expected_prefix": "SL-", "new_prefix": "SLG-"}
        result = {"supplier": "sloger", "product_prefix": "SLG-", "retired_prefixes": ["SL-"], "replayed": False}
        with patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr(token)), \
                patch.object(supplier_prefixes, "rotate_supplier_prefix", return_value=result) as rotate:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://hub.example.test") as client:
                denied = await client.post("/suppliers/sloger/prefix-rotation", json=payload)
                self.assertEqual(denied.status_code, 401)
                rotate.assert_not_called()
                headers = {"Authorization": "Bearer " + token}
                invalid = await client.post("/suppliers/sloger/prefix-rotation", headers=headers,
                                            json={**payload, "new_prefix": "../invalid"})
                self.assertEqual(invalid.status_code, 422)
                rotate.assert_not_called()
                accepted = await client.post("/suppliers/sloger/prefix-rotation", headers=headers, json=payload)
                self.assertEqual(accepted.status_code, 200)
                self.assertEqual(accepted.headers["cache-control"], "no-store")
                self.assertEqual(accepted.json(), result)
                rotate.assert_called_once_with("sloger", "SL-", "SLG-")


if __name__ == "__main__":
    unittest.main()
