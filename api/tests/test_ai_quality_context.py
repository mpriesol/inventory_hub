import copy
import json
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from catalog_fixtures import product
from test_ai_existing import remote_product
from test_ai_rule_content import reviewed, source
from inventory_hub.ai_content_types import CategoryProfile, RuleBook, Scope
from inventory_hub.services import ai_content as service, ai_content_provider as provider
from inventory_hub.services import ai_content_references as references, ai_content_rules as rules
from inventory_hub.services.ai_content_sources import public_document_url
from inventory_hub.services import ai_rule_content as compiler


class QualityContextTests(unittest.TestCase):
    def test_reference_parameter_rows_match_the_output_schema_without_mutating_snapshot(self):
        content = {"title": "Example", "parameters": [
            {"name": "Priemer kolesa", "value": "27,5″"},
            {"name": "Priemer kolesa", "value": "584 mm"},
            {"name": "Priemer kolesa", "value": "584 mm"}]}
        frozen = copy.deepcopy(content)
        result = provider.reference_content(content)
        self.assertEqual(result["parameters"], [{"name": "Priemer kolesa", "values": ["27,5″", "584 mm"], "product_id": None}])
        self.assertEqual(content, frozen)

    def test_tube_dimension_normalization_preserves_conflicts_scope_and_frozen_jobs(self):
        rows = [{"name": name, "values": [value], "product_id": id} for name, value, id in (
            ("Priemer kolesa", "27,5″", None), ("Priemer kolesa", "584 mm", None),
            ("Dĺžka ventilu", "40 mm", None), ("Dĺžka ventilu", "60 mm", None),
            ("Priemer kolesa", "20″", 1), ("Unknown", "A", None), ("Unknown", "B", None))]
        content = {"parameters": rows, "warnings": []}
        frozen = copy.deepcopy(content)
        context = {"prompt_version": 3, "resolved": {"category": {"id": "inner_tubes", "parameters": [
            {"name": name, "approved": True, "scope": "parent"} for name in ("Priemer kolesa", "Dĺžka ventilu")]}}}
        result = provider.normalize_dimension_parameters(content, context)
        self.assertEqual(result["parameters"][0]["values"], ["27,5″", "584 mm"])
        self.assertEqual(result["parameters"][1:], rows[2:])
        self.assertEqual(content, frozen)
        self.assertTrue(result["warnings"])
        self.assertIs(provider.normalize_dimension_parameters(content, {**context, "prompt_version": 2}), content)
        self.assertIs(provider.normalize_dimension_parameters(content, {"prompt_version": 3, "resolved": {"category": {"id": "bikes"}}}), content)
        full_warnings = {**content, "warnings": ["Existing warning"] * 100}
        self.assertIs(provider.normalize_dimension_parameters(full_warnings, context), full_warnings)

        from test_ai_content import content as sample_content, context as sample_context
        from inventory_hub.services.ai_content_validation import validate_content
        checked_context = sample_context(resolved={"category": {"parameters": [
            {"name": name, "approved": True, "scope": "parent", "required": False, "values": []}
            for name in ("Priemer kolesa", "Dĺžka ventilu")]}})
        checks = validate_content(sample_content(parameters=result["parameters"]), checked_context, [])
        self.assertIn("ai_duplicate_parameter:Dĺžka ventilu", checks["errors"])
        self.assertIn("ai_parameter_scope:Priemer kolesa", checks["errors"])
        self.assertIn("ai_unregistered_parameter:Unknown", checks["errors"])

    def test_new_luna_high_and_old_frozen_low_have_separate_token_budgets(self):
        context = {"model": "gpt-6-luna", "shop": "biketrek", "options": {"language": "sk"},
                   "facts": [], "resolved": {"instructions": [], "category": None}, "research": "feed_only",
                   "classification_catalog": {"choices": [], "profiles": []}}
        self.assertEqual(provider.request_body(context)["reasoning"]["effort"], "low")
        current = {**context, **provider.generation_settings("gpt-6-luna")}
        for kind in ("product", "classification"):
            body = provider.request_body(current, kind)
            self.assertEqual(body["reasoning"]["effort"], "high")
            self.assertGreater(body["max_output_tokens"], provider.request_body(context, kind)["max_output_tokens"])
        self.assertGreater(provider.estimate(current), provider.estimate(context))
        self.assertEqual(provider.generation_settings("gpt-5.6-sol")["reasoning_effort"], "low")

    def test_reasoning_tokens_are_not_double_charged(self):
        response = {"usage": {"input_tokens": 1000, "output_tokens": 2000,
            "output_tokens_details": {"reasoning_tokens": 1500}}, "output": []}
        usage, cost = provider.usage_cost(response, "gpt-6-luna")
        self.assertEqual(usage["reasoning_tokens"], 1500)
        self.assertEqual(cost, Decimal("0.001100"))

    def test_feed_tables_preserve_cells_without_exposing_unrelated_raw_data(self):
        p = product(); p.supplier = "paul-lange"
        p.static_parameters = {"SIZE_TABLE": ['<table><tr><td>Ventil</td><td>AV40</td></tr><tr><td></td><td>47-507 (24x1.90)</td></tr></table>'],
            "STA_URL1": ["https://supplier.example.org/manual.pdf"], "SECRET": "NEVER_FORWARD"}
        old = service.source_digest([p])
        facts = service.facts([p], technical=True)[0]
        self.assertEqual(facts["technical_tables"][0]["rows"][1], ["", "47-507 (24x1.90)"])
        self.assertEqual(facts["source_documents"][0]["url"], "https://supplier.example.org/manual.pdf")
        self.assertNotIn("NEVER_FORWARD", json.dumps(facts))
        self.assertNotIn("technical_tables", service.facts([p])[0])
        before = service.source_digest([p], technical=True)
        p.static_parameters["SIZE_TABLE"] = ['<table><tr><td>Ventil</td><td>AV60</td></tr></table>']
        self.assertNotEqual(before, service.source_digest([p], technical=True))
        self.assertEqual(old, service.source_digest([p]), "Legacy jobs retain their old source digest")
        for url in ("https://user:secret@example.com/x.pdf", "https://example.com/x.pdf?token=secret",
                    "https://127.0.0.1/x.pdf", "https://host.internal/x.pdf", "file:///x.pdf"):
            self.assertIsNone(public_document_url(url))

    def test_reference_is_content_only_and_bound_to_category_shop_and_snapshot(self):
        remote = remote_product()
        remote["metas"] = [{"key": "future_name", "value": "MODEL"}, {"key": "private", "value": "NEVER_FORWARD"}]
        reference = references.snapshot(remote, "biketrek")
        self.assertEqual(reference["content"]["long_description"], "<p>Aluminium body</p>")
        self.assertEqual(reference["content"]["future_name"], "MODEL")
        for secret in ("buy_price", "stock", "NEVER_FORWARD", "not-for-ai", "ean", "variant_id"):
            self.assertNotIn(secret, json.dumps(reference))
        book = RuleBook(categories=[CategoryProfile(id="pump", name="Pump", reference_products=[reference]),
                                    CategoryProfile(id="tube", name="Tube")])
        result = rules.resolve(book, Scope(shop="biketrek", category="pump", product="NEW"))
        self.assertEqual(result["reference_products"], [reference])
        self.assertNotIn("reference_products", result["category"])
        for scope in (Scope(shop="xtrek", category="pump"), Scope(shop="biketrek", category="tube"),
                      Scope(shop="biketrek", category="pump", product=remote["code"])):
            self.assertEqual(rules.resolve(book, scope)["reference_products"], [])
        book.categories[0].reference_products[0].content.title = "Changed later"
        self.assertNotEqual(result["reference_products"][0]["content"]["title"], "Changed later")
        context = {"model": "gpt-6-luna", "shop": "biketrek", "options": {"language": "sk"},
                   "facts": [{"name": "NEW"}], "resolved": result, "research": "feed_only",
                   **provider.generation_settings("gpt-6-luna")}
        data = json.loads(provider.request_body(context)["input"])
        self.assertEqual(data["facts"], [{"name": "NEW"}])
        self.assertEqual(data["reference_examples"][0]["content"]["future_name"], "MODEL")
        self.assertNotIn("code", data["reference_examples"][0])
        legacy = json.loads(provider.request_body({**context, "prompt_version": 2})["input"])
        self.assertEqual(legacy["reference_examples"][0]["content"], reference["content"])

    def test_deduplication_removes_only_wholly_repeated_registered_meaning(self):
        row = source("03-7-1", "### 7.1 Register\nIMPORTANT OUTSIDE TABLE\n\n| Parameter | Format | Scope |\n|---|---|---|\n| Ventil | `AV` | Exact SKU |\n| Material | Butyl | Do not assume |\n\nTAIL\n")
        category = {"parameters": [{"name": "Ventil", "approved": True, "instructions": "AV | Exact SKU"},
                                  {"name": "Material", "approved": True, "instructions": "CHANGED"}]}
        with reviewed([row]):
            selected, audit = compiler.compile_instructions([row], category, "biketrek")
        text = selected[0]["text"]
        self.assertNotIn("| Ventil |", text)
        self.assertIn("| Material | Butyl | Do not assume |", text)
        self.assertIn("IMPORTANT OUTSIDE TABLE", text)
        self.assertIn("TAIL", text)
        self.assertIn("| Parameter |", text)


class ReferenceReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_loading_example_is_exact_read_without_generating_content(self):
        client = object()
        request = references.ReferenceRequest(shop="biketrek", code="REAL-SHOP-CODE")
        with patch.object(references, "shop_config"), patch.object(references.UpgatesClient, "from_shop", return_value=client), \
             patch.object(references, "read_product", return_value=remote_product()) as read, \
             patch.object(provider, "generate", AsyncMock()) as generate:
            result = await references.load(request)
        read.assert_called_once_with(client, "REAL-SHOP-CODE", include_parameters=True)
        generate.assert_not_called()
        self.assertEqual(result["code"], "REAL-SHOP-CODE")
