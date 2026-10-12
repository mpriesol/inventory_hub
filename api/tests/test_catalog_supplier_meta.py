import unittest
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from inventory_hub.services import ai_content_upgates as fields
from inventory_hub.services.catalog import CatalogError
from inventory_hub.services.catalog_supplier_meta import attach_meta, prepare_meta, verify_payload


class SupplierMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rows = []
        self.saved = None
        self.posts = []

        @contextmanager
        def cache(*args):
            yield Path(self.temp.name) / "fields.json", self.saved, "connection-fixture"

        self.addCleanup(patch.stopall)
        patch.object(fields.imports, "_shop_cache", cache).start()
        self.pages = patch.object(fields.imports, "_pages", side_effect=lambda *args: deepcopy(self.rows)).start()

        class Client:
            def post(client, path, body):
                self.posts.append((path, deepcopy(body)))
                self.rows.extend(deepcopy(body["metas"]))

        self.client = Client()

    def definition(self, *, common=False, type="input", key="supplier_name"):
        return {"key": key, "category": "products", "type": type,
                "common_languages_value_yn": common}

    def test_preview_is_read_only_and_ignores_unrelated_incompatible_ai_field(self):
        self.rows = [self.definition(type="checkbox", key="h1_descriptor")]
        meta = prepare_meta({}, self.client, "Fixture supplier", "sk")
        self.assertEqual(meta, {"key": "supplier_name", "values": [{"language": "sk", "value": "Fixture supplier"}]})
        self.assertEqual(self.posts, [])

    def test_create_adds_and_confirms_only_supplier_field(self):
        prepare_meta({}, self.client, "Fixture supplier", "sk", create=True)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0][0], "metas")
        self.assertEqual([m["key"] for m in self.posts[0][1]["metas"]], ["supplier_name"])
        self.assertEqual(self.pages.call_count, 2)

    def test_existing_textarea_and_shared_values_are_preserved(self):
        self.rows = [self.definition(common=True, type="textarea")]
        self.assertEqual(prepare_meta({}, self.client, "Fixture supplier", "sk", create=True),
                         {"key": "supplier_name", "value": "Fixture supplier"})
        self.assertEqual(self.posts, [])
        self.assertEqual(self.rows[0]["type"], "textarea")

    def test_publish_rechecks_definition_even_with_fresh_preview_cache(self):
        self.saved = {"checked_at": fields.imports.now().isoformat(), "data": [self.definition(common=False)]}
        self.rows = [self.definition(common=True)]
        preview = prepare_meta({}, self.client, "Fixture supplier", "sk")
        current = prepare_meta({}, self.client, "Fixture supplier", "sk", create=True)
        self.assertIn("values", preview)
        self.assertIn("value", current)
        self.assertEqual(self.pages.call_count, 1)

    def test_incompatible_supplier_definition_blocks_without_writes(self):
        self.rows = [self.definition(type="checkbox")]
        with self.assertRaises(CatalogError) as error:
            prepare_meta({}, self.client, "Fixture supplier", "sk", create=True)
        self.assertEqual(error.exception.code, "ai_meta_incompatible")
        self.assertEqual(self.posts, [])

    def test_missing_supplier_name_does_not_read_or_create_fields(self):
        with self.assertRaises(CatalogError):
            prepare_meta({}, self.client, "  ", "sk", create=True)
        self.pages.assert_not_called()
        self.assertEqual(self.posts, [])

    def test_attach_overrides_ai_name_for_parent_and_variants_only(self):
        payload = {"code": "EXISTING", "metas": [{"key": "supplier_name", "value": "Generated"},
                    {"key": "validation_required", "value": "1"}],
                   "variants": [{"code": "EXISTING-S", "metas": [{"key": "future_name", "value": "Model"}]}]}
        meta = {"key": "supplier_name", "values": [{"language": "sk", "value": "Fixture supplier"}]}
        attach_meta(payload, meta)
        self.assertEqual(payload["code"], "EXISTING")
        self.assertEqual(payload["variants"][0]["code"], "EXISTING-S")
        self.assertEqual(payload["metas"][0], {"key": "validation_required", "value": "1"})
        self.assertEqual(payload["variants"][0]["metas"][0], {"key": "future_name", "value": "Model"})
        self.assertEqual(payload["metas"][-1], meta)
        self.assertEqual(payload["variants"][0]["metas"][-1], meta)
        payload["variants"][0]["metas"][-1]["values"][0]["value"] = "Changed"
        self.assertEqual(payload["metas"][-1], meta)

    def test_readback_checks_target_language_and_every_variant(self):
        expected = {"code": "P", "variants": [{"code": "V"}]}
        attach_meta(expected, {"key": "supplier_name", "values": [{"language": "sk", "value": "Fixture supplier"}]})
        remote = deepcopy(expected)
        remote["metas"] = [{"key": "supplier_name", "values": {"sk": "Fixture supplier"}}]
        remote["variants"][0]["metas"] = [{"key": "supplier_name", "value": "Fixture supplier"}]
        verify_payload(remote, expected, "sk")
        for modified in (
            {**remote, "metas": [{"key": "supplier_name", "values": {"en": "Fixture supplier"}}]},
            {**remote, "variants": []},
            {**remote, "variants": [{"code": "V", "metas": [{"key": "supplier_name", "value": "Other"}]}]},
        ):
            with self.subTest(remote=modified), self.assertRaises(CatalogError) as error:
                verify_payload(modified, expected, "sk")
            self.assertEqual(error.exception.code, "import_supplier_readback_mismatch")

    def test_historical_payload_without_supplier_metadata_keeps_recovery_compatible(self):
        verify_payload({"code": "P"}, {"code": "P", "variants": [{"code": "V"}]}, "sk")


if __name__ == "__main__":
    unittest.main()
