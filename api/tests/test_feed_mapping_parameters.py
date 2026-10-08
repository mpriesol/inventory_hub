"""Parameter mappings preserve name/value pairs and use read-only target registers."""
from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from catalog_fixtures import product
from inventory_hub.ai_content_types import RuleBook
from inventory_hub.feed_mapping_types import MappingDefinition
from inventory_hub.services import ai_content_upgates, feed_mapping, feed_mapping_parameters
from inventory_hub.services.catalog import CatalogError


CFG = {"default_currency": "EUR", "adapter_settings": {"vat": 23, "mapping": {"postprocess": {"product_code_prefix": "PL-"}}}}


def definition(**overrides):
    return MappingDefinition.model_validate({"bindings": [{
        "target": "parameter:Ventil", "source": "DYN_PARAMS/PARAM", "param_name_path": "DESC",
        "param_value_path": "VAL", "param_match_name": "Ventil duše", **overrides}]})


class NamedParameterMappingTests(unittest.TestCase):
    def test_pair_selection_is_independent_of_order_and_transforms_selected_values(self):
        mapping = definition(transforms=[{"op": "map", "values": {"AV": "Auto", "SV": "Presta"}}])
        pairs = [{"DESC": ["Materiál"], "VAL": ["Butyl"]},
                 {"DESC": ["Ventil duše"], "VAL": ["AV", "SV"]},
                 {"DESC": ["Iný ventil"], "VAL": ["DV"]}]
        for ordered in (pairs, list(reversed(pairs))):
            result = feed_mapping.apply_definition(product(), {"DYN_PARAMS": {"PARAM": ordered}},
                                                   mapping, "paul-lange", "products", CFG)
            self.assertEqual([p.value for p in result.parameters if p.name == "Ventil"], ["Auto", "Presta"])
            self.assertEqual(result.mapping_provenance["fields"]["parameter:Ventil"]["param_match_name"], "Ventil duše")

    def test_missing_source_preserves_native_parameters_and_exact_match_does_not_guess(self):
        original = product()
        raw = {"DYN_PARAMS": {"PARAM": [{"DESC": "ventil duše", "VAL": "AV"}]}}
        result = feed_mapping.apply_definition(original, raw, definition(), "paul-lange", "products", CFG)
        self.assertEqual(result.parameters, original.parameters)
        result = feed_mapping.apply_definition(original, raw, definition(default="Unknown"), "paul-lange", "products", CFG)
        self.assertEqual(result.parameters[-1].model_dump(), {"name": "Ventil", "value": "Unknown"})

    def test_malformed_paired_values_and_incomplete_binding_are_rejected(self):
        for row in ({"DESC": ["Ventil duše", "Materiál"], "VAL": "AV"},
                    {"DESC": "Ventil duše", "VAL": {"nested": "AV"}}):
            with self.assertRaises(ValueError):
                feed_mapping.apply_definition(product(), {"DYN_PARAMS": {"PARAM": [row]}},
                                              definition(transforms=[{"op": "split", "value": ","}]), "paul-lange", "products", CFG)
        for override in ({"target": "name"}, {"param_name_path": None}, {"param_value_path": None},
                         {"source": None}, {"param_match_name": "  "}, {"param_match_name": "name\n"}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                feed_mapping.validate_definition(definition(**override))


class ParameterOptionsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.book = RuleBook.model_validate({"categories": [{"id": "tubes", "name": "Duše",
            "shop_categories": {"biketrek": "LEAF"}, "shop_category_matches": {"biketrek": ["LEAF", "SECOND"]},
            "parameters": [{"name": "Ventil", "values": ["Auto", "Presta"], "required": True},
                           {"name": "Nepotvrdený", "approved": False}]}]})
        self.db = Mock()
        self.db.scalar = AsyncMock(return_value=1)
        self.db.get = AsyncMock(side_effect=[SimpleNamespace(published_id=15), SimpleNamespace(id=15, book=self.book.model_dump())])
        self.config = patch.object(feed_mapping_parameters.catalog, "supplier_config", return_value=CFG)
        self.config.start()
        self.addCleanup(self.config.stop)

    async def test_shop_registry_and_published_category_choices_are_separate(self):
        registry = {"checked_at": "2026-10-08T12:00:00+00:00", "parameters": [
            {"id": 1, "names": {"sk": "Ventil", "en": "Valve"}}, {"id": 2, "names": {"sk": "bad\nname"}}]}
        with patch.object(feed_mapping_parameters.catalog_import, "shop_config"), \
             patch.object(feed_mapping_parameters.UpgatesClient, "from_shop", return_value=Mock()), \
             patch.object(feed_mapping_parameters.ai_content_upgates, "parameter_registry", return_value=registry) as read:
            result = await feed_mapping_parameters.parameter_options(self.db, "paul-lange", shop="biketrek", refresh=True)
        self.assertEqual(result["parameters"], registry["parameters"][:1])
        self.assertEqual(result["rules_version"], 15)
        self.assertEqual(result["category_profiles"][0]["category_codes"], ["LEAF", "SECOND"])
        self.assertEqual([p["name"] for p in result["category_profiles"][0]["parameters"]], ["Ventil"])
        self.assertTrue(read.call_args.kwargs["refresh"])
        self.db.add.assert_not_called()

    async def test_unavailable_shop_keeps_category_register_without_exposing_exception(self):
        with patch.object(feed_mapping_parameters.catalog_import, "shop_config",
                          side_effect=CatalogError("shop_not_ready", "Sensitive upstream details", 422)):
            result = await feed_mapping_parameters.parameter_options(self.db, "paul-lange", shop="biketrek")
        self.assertEqual(result["warnings"], ["upgates_parameter_registry_unavailable"])
        self.assertTrue(result["category_profiles"])
        self.assertNotIn("Sensitive", str(result))

    async def test_uninitialized_read_does_not_seed_rules_or_load_remote_catalog(self):
        self.db.get = AsyncMock(return_value=None)
        with patch.object(feed_mapping_parameters.ai_content_upgates, "parameter_registry") as read:
            result = await feed_mapping_parameters.parameter_options(self.db, "paul-lange")
        self.assertIsNone(result["rules_version"])
        self.assertEqual(result["category_profiles"], [])
        self.db.add.assert_not_called()
        read.assert_not_called()


class ParameterRegistryRefreshTests(unittest.TestCase):
    def test_explicit_refresh_bypasses_fresh_cache_without_writing_shop_parameters(self):
        saved = {"checked_at": ai_content_upgates.imports.now().isoformat(), "data": [{"id": 1, "names": {"sk": "Old"}}]}
        @contextmanager
        def cache(*args):
            yield "unused", saved, "fingerprint"
        client = Mock()
        with patch.object(ai_content_upgates.imports, "_shop_cache", cache), \
             patch.object(ai_content_upgates.imports, "_pages", return_value=[{"id": 2, "descriptions": [{"language": "sk", "name": "New"}]}]) as pages, \
             patch.object(ai_content_upgates.imports, "_write"):
            cached = ai_content_upgates.parameter_registry("biketrek", client)
            pages.assert_not_called()
            refreshed = ai_content_upgates.parameter_registry("biketrek", client, refresh=True)
        self.assertEqual(cached["parameters"][0]["names"]["sk"], "Old")
        self.assertEqual(refreshed["parameters"][0]["names"]["sk"], "New")
        client.post.assert_not_called()
        client.put.assert_not_called()


if __name__ == "__main__":
    unittest.main()
