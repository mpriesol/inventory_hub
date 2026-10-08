"""Offline packaging contracts, using synthetic rules and no live services."""
import copy
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from inventory_hub.ai_content_types import Rule, RuleBook, Scope
from inventory_hub.services.ai_rule_package import (
    PackageError, compile_draft, export_package, load_packages, split_text, validate_packages,
)
from inventory_hub.services import ai_content_provider as provider


def package(text="Úplné pravidlo.\n"):
    return [{"documents": [{"id": "01", "name": "Hlavné pravidlá", "revision": 14, "line_count": 2}],
        "modules": [
            {"id": "common", "name": "Spoločné", "document": "01", "revision": "1.1 rev14",
             "section": "1", "kind": "rule", "status": "active", "scope": {"shop": "biketrek"},
             "text": text, "source_lines": [1, 1]},
            {"id": "history", "name": "História", "document": "01", "revision": "1.1 rev14",
             "section": "history", "kind": "history", "status": "active", "scope": {"shop": "biketrek"},
             "text": "Staré pravidlo.", "source_lines": [2, 2]}],
        "profiles": [{"id": "general", "name": "Všeobecný produkt", "status": "active", "module_ids": ["common"]}],
        "private_metadata": {"mapping_note": "Keep this without guessing a shop code"}}]


def compile_(packages, **kwargs):
    return compile_draft(packages, shop="biketrek", supplier="paul-lange", brand="TEST", profile="general", **kwargs)


class RulePackageTests(unittest.TestCase):
    def test_provider_editorial_policy_is_limited_to_biketrek(self):
        context = {"model": "gpt-5.6-sol", "options": {"language": "sk"}, "facts": [],
                   "research": "official", "resolved": {"instructions": [], "category": None}}
        # Shared product instruction, including the static HTML guidance for all
        # shops; BIKETREK-specific editorial rules must not leak to other shops.
        shared_product = "bf71a2d0a6bee6310283749aa2ad0bb434fd4c4c8721aba694533014da395d69"
        for shop in ("xtrek", "another-shop"):
            instruction = provider.request_body({**context, "shop": shop})["instructions"]
            self.assertEqual(hashlib.sha256(instruction.encode()).hexdigest(), shared_product)
        biketrek = provider.request_body({**context, "shop": "biketrek"})["instructions"]
        self.assertIn("bez druhého webového potvrdzovania", biketrek)
        self.assertIn("Aktívne vysvetľuj spoľahlivé pozitívne prínosy", biketrek)
        self.assertNotIn("Každý použitý zdroj a doslovný podklad eviduj.", biketrek)
        for shop in ("biketrek", "xtrek"):
            proposal = provider.request_body({**context, "shop": shop, "current": {},
                                              "proposal_request": "Upraviť vlastné pravidlo"}, kind="rules")
            self.assertEqual(hashlib.sha256(proposal["instructions"].encode()).hexdigest(),
                             "7bd34f271c27d6082a78c84ac48467f8018d4ee5b985a1e04598ff373953aa9e")

    def test_export_preserves_metadata_history_and_checksums(self):
        source = package()
        result = export_package(source)
        self.assertEqual(result["packages"], source)
        self.assertTrue(result["report"]["coverage"]["01"]["complete"])
        self.assertFalse(result["report"]["coverage"]["01"]["source_hash_verified"])
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "bundle.json"
            file.write_text(json.dumps(result))
            self.assertEqual(load_packages(tmp), source)
            result["packages"][0]["modules"][0]["text"] = "tampered"
            file.write_text(json.dumps(result))
            with self.assertRaisesRegex(PackageError, "package_checksum_mismatch"):
                load_packages(tmp)

    def test_full_read_preserves_intro_and_history_as_nonoperative_context(self):
        source = package()
        source[0]["documents"][0].update(required_full_read=True, line_count=3)
        source[0]["modules"][0]["source_lines"] = [2, 2]
        source[0]["modules"][1]["source_lines"] = [3, 3]
        intro = {**source[0]["modules"][0], "id": "intro", "kind": "reference", "text": "Úvodný kontext.\n", "source_lines": [1, 1]}
        source[0]["modules"].append(intro)
        source[0]["documents"].append({"id": "02", "name": "Other document", "line_count": 1})
        source[0]["modules"].append({**intro, "id": "other-reference", "document": "02", "text": "Unrelated reference"})
        result = compile_(source)
        selected = result["report"]["selected_modules"]
        self.assertEqual(selected, ["intro", "common", "history"])
        restored = "".join(rule["instructions"].split("\n", 2)[2] for rule in result["book"]["rules"])
        self.assertEqual(restored, intro["text"] + source[0]["modules"][0]["text"] + source[0]["modules"][1]["text"])
        for rule in result["book"]["rules"]:
            header = rule["instructions"].split("\n", 1)[0]
            self.assertIn("stav:active", header)
            if "typ:history" in header or "typ:reference" in header:
                self.assertIn("nevytvára účinné pravidlá", header)
                self.assertIn("staršie pokyny sa neuplatňujú", header)
        source[0]["modules"][1]["status"] = "draft"
        with self.assertRaisesRegex(PackageError, "package_module_unapproved"):
            compile_(source)
        source[0]["documents"][0]["required_full_read"] = "true"
        with self.assertRaisesRegex(PackageError, "package_schema"):
            validate_packages(source)

    def test_duplicate_and_missing_references_fail(self):
        source = package()
        source[0]["modules"].append(copy.deepcopy(source[0]["modules"][0]))
        with self.assertRaisesRegex(PackageError, "package_duplicate_id"):
            validate_packages(source)
        source = package()
        source[0]["profiles"][0]["knowledge_ids"] = ["missing"]
        with self.assertRaisesRegex(PackageError, "package_reference_missing"):
            validate_packages(source)

    def test_draft_and_mixed_never_become_instructions(self):
        for status in ("draft", "mixed"):
            source = package()
            source[0]["modules"][0]["status"] = status
            with self.assertRaisesRegex(PackageError, "package_module_unapproved"):
                compile_(source)
            source = package()
            source[0]["profiles"][0]["status"] = status
            with self.assertRaisesRegex(PackageError, "package_profile_unapproved"):
                compile_(source)

    def test_chunks_are_lossless_and_full_rules_over_old_limit_fit(self):
        source_text = ("Potvrdená vlastnosť → vysvetlenie.\n\n" * 5000) + "KONIEC"
        chunks = split_text(source_text, 23000)
        self.assertEqual("".join(chunks), source_text)
        self.assertTrue(all(len(chunk) <= 23000 for chunk in chunks))
        compiled = compile_(package(source_text))
        self.assertGreater(compiled["report"]["prompt_bytes_without_product_facts"], 100000)
        self.assertEqual(compiled["report"]["prompt_limit_bytes"], provider.MAX_PROMPT_BYTES)
        self.assertEqual(compiled["report"]["selected_modules"], ["common"])
        book = RuleBook.model_validate(compiled["book"])
        recovered = "".join(rule.instructions.split("\n", 2)[2] for rule in book.rules)
        self.assertEqual(recovered, source_text)
        self.assertTrue(all(len(rule.instructions) <= 24000 for rule in book.rules))
        self.assertFalse(book.categories[0].automatic_import_ready)
        self.assertEqual(compiled["report"]["status"], "review_draft")
        self.assertFalse(compiled["report"]["published"])

    def test_full_request_limit_fails_without_truncation(self):
        with self.assertRaisesRegex(PackageError, "package_prompt_too_large"):
            compile_(package("á" * 300000))

    def test_existing_other_shop_preserved_applicable_rules_rejected(self):
        book = RuleBook(rules=[Rule(id="xtrek-only", name="xTrek", scope=Scope(shop="xtrek"), instructions="xTrek rules")])
        result = compile_(package(), current_book=book.model_dump())
        self.assertEqual(result["book"]["rules"][0], book.rules[0].model_dump())
        book.rules.append(Rule(id="legacy", name="Legacy", instructions="Old global rules"))
        with self.assertRaisesRegex(PackageError, "package_existing_scope_conflict"):
            compile_(package(), current_book=book.model_dump())

    def test_material_knowledge_is_explicit_and_wrong_scopes_rejected(self):
        source = package()
        module = copy.deepcopy(source[0]["modules"][0])
        module.update(id="material", kind="knowledge", text="Confirmed material benefits.",
                      selection_requires_confirmed_facts=True, selection_terms=["merino"])
        source[0]["modules"].append(module)
        self.assertNotIn("material", compile_(source)["report"]["selected_modules"])
        with self.assertRaisesRegex(PackageError, "package_product_scope_required"):
            compile_(source, knowledge_ids=["material"])
        self.assertIn("material", compile_(source, knowledge_ids=["material"], product="PL-MODEL")["report"]["selected_modules"])
        module["scope"]["brand"] = "Different"
        with self.assertRaisesRegex(PackageError, "package_scope_conflict"):
            compile_(source, knowledge_ids=["material"], product="PL-MODEL")

    def test_category_selection_annotation_is_honored(self):
        source = package()
        module = copy.deepcopy(source[0]["modules"][0])
        module.update(id="clothing", text="Clothing-specific only")
        source[0]["modules"].append(module)
        source[0]["module_annotations"] = {"clothing": {"selection_categories": ["clothing_od_01"]}}
        self.assertNotIn("clothing", compile_(source)["report"]["selected_modules"])

    def test_optional_profile_knowledge_does_not_block_without_confirmed_fact(self):
        source = package()
        module = copy.deepcopy(source[0]["modules"][0])
        module.update(id="spd", kind="knowledge", selection_requires_confirmed_facts=True, selection_terms=["SPD"])
        source[0]["modules"].append(module)
        source[0]["profiles"][0]["knowledge_ids"] = ["spd"]
        self.assertNotIn("spd", compile_(source)["report"]["selected_modules"])
        self.assertIn("spd", compile_(source, knowledge_ids=["spd"], product="PL-MODEL")["report"]["selected_modules"])

    def test_confirmed_knowledge_cannot_match_other_products(self):
        from inventory_hub.services.ai_content_rules import resolve
        source = package()
        module = copy.deepcopy(source[0]["modules"][0])
        module.update(id="material", kind="knowledge", selection_requires_confirmed_facts=True)
        source[0]["modules"].append(module)
        result = compile_(source, knowledge_ids=["material"], product="PL-PARENT")
        book = RuleBook.model_validate(result["book"])
        scope = dict(shop="biketrek", supplier="paul-lange", brand="TEST", category="general")
        self.assertTrue(resolve(book, Scope(**scope, product="PL-PARENT"))["instructions"])
        self.assertEqual(resolve(book, Scope(**scope, product="PL-OTHER"))["instructions"], [])
        self.assertEqual(resolve(book, Scope(**scope))["instructions"], [])
        self.assertTrue(all(rule.scope.product == "PL-PARENT" for rule in book.rules))

    def test_product_scoped_source_requires_explicit_runtime_code(self):
        source = package()
        source[0]["modules"][0]["scope"]["product"] = "PL-PARENT"
        with self.assertRaisesRegex(PackageError, "package_product_scope_required"):
            compile_(source)
        self.assertEqual(compile_(source, product="PL-PARENT")["report"]["selected_modules"], ["common"])

    def test_aliases_select_sources_without_rewriting_runtime_identity(self):
        from inventory_hub.services.ai_content_rules import resolve
        source = package()
        source[0]["aliases"] = {"brand": {"BBB Cycling": ["BBB"]}, "supplier": {"paul-lange": ["PL"]}}
        source[0]["modules"][0]["scope"].update(brand="BBB Cycling", supplier="paul-lange")
        result = compile_draft(source, shop="biketrek", supplier="PL", brand="BBB", profile="general")
        book = RuleBook.model_validate(result["book"])
        self.assertTrue(resolve(book, Scope(shop="biketrek", supplier="PL", brand="BBB", category="general"))["instructions"])
        self.assertTrue(all(rule.scope.brand == "BBB" and rule.scope.supplier == "PL" for rule in book.rules))
        self.assertEqual(result["report"]["source_selection_context"]["brand"], "BBB Cycling")

    def test_invalid_current_book_errors_never_print_private_content(self):
        from inventory_hub.ai_rule_package import main
        marker = "PRIVATE_MARKER_87319"
        for patch in ({"official_domains": [f"https://u:{marker}@a.co"]},
                      {"instructions": marker + "x" * 24000},
                      {"official_domains": [marker]}):
            book = {"rules": [{"id": "old", "name": "Old", "scope": {"shop": "xtrek"}, **patch}]}
            with self.assertRaises(PackageError) as raised:
                compile_(package(), current_book=book)
            self.assertNotIn(marker, str(raised.exception))
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "sources").mkdir()
                (root / "sources" / "source.json").write_text(json.dumps(package()[0]))
                (root / "current.json").write_text(json.dumps(book))
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    code = main(["draft", "--package-dir", str(root / "sources"), "--current-book",
                                 str(root / "current.json"), "--output", str(root / "draft.json")])
                self.assertEqual(code, 2)
                self.assertNotIn(marker, stderr.getvalue())
                self.assertFalse((root / "draft.json").exists())

    def test_required_dependency_cannot_escape_category_or_supplier_scope(self):
        for scope in ({"supplier": "another"}, {"shop": "xtrek"}):
            source = package()
            module = copy.deepcopy(source[0]["modules"][0])
            module.update(id="dependency", scope=scope)
            source[0]["modules"].append(module)
            source[0]["module_annotations"] = {"common": {"required_modules": ["dependency"]}}
            with self.assertRaisesRegex(PackageError, "package_scope_conflict"):
                compile_(source)
        source = package()
        source[0]["modules"][0]["selection_categories"] = ["different"]
        with self.assertRaisesRegex(PackageError, "package_scope_conflict"):
            compile_(source)

    def test_complete_larger_rules_increase_estimate_without_disabling_limits(self):
        small = compile_(package())
        large = compile_(package("Pravidlo. " * 20000))
        self.assertGreater(large["report"]["prompt_bytes_without_product_facts"], small["report"]["prompt_bytes_without_product_facts"])
        from inventory_hub.services.ai_content_rules import resolve
        context = {"model": "gpt-5.6-sol", "shop": "biketrek", "options": {"language": "sk"}, "facts": [], "research": "official"}
        selected = Scope(shop="biketrek", supplier="paul-lange", brand="TEST", category="general")
        amounts = [provider.estimate({**context, "resolved": resolve(RuleBook.model_validate(result["book"]), selected)}) for result in (small, large)]
        self.assertGreater(amounts[1], amounts[0])

    def test_credential_detection_does_not_echo_secret(self):
        with self.assertRaises(PackageError) as raised:
            validate_packages(package("https://user:PRIVATE_SECRET@example.test/feed"))
        self.assertEqual(raised.exception.code, "package_credentials")
        self.assertNotIn("PRIVATE_SECRET", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
