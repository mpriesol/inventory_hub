"""Content compilation contracts; synthetic source books, no paid requests."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from inventory_hub.ai_content_types import CategoryProfile, Rule, RuleBook, Scope
from inventory_hub.services import ai_content_provider as provider, ai_content_rules as rules
from inventory_hub.services import ai_rule_content as compiler


def source(module, body, *, revision=None, kind="rule"):
    document = module.split("-", 1)[0]
    revision = revision or {"01": "1.1 rev14", "03": "1.1 rev13"}.get(document, "1.0 rev17")
    return {"id": module.replace("-", "_"), "name": module,
            "text": f"Podklad: {document} | {revision} | test | {module} | typ:{kind} | stav:active\n{body}"}


def reviewed(rows):
    return patch.dict(compiler._REVIEWED, {row["name"]: hashlib.sha256(row["text"].encode()).hexdigest() for row in rows})


def category(profile="inner_tubes", *parameters):
    return {"id": profile, "parameters": [{"name": name, "approved": True} for name in parameters]}


class ContentCompilationTests(unittest.TestCase):
    def test_transport_instructions_are_removed_without_losing_content_fields(self):
        xml = source("01-11", "## 11. XML\n\n### 11.1 Formát\nHTML dlhého popisu v CDATA; krátky popis, názov a SEO polia sú čistý text.\n\n"
                     "### 11.2 Mapovanie\nUNSUPPORTED_XML_MAP\n### 11.3 Príklad\n<PRODUCTS>FICTITIOUS_PRODUCT</PRODUCTS>\n")
        weight = source("01-8", "## 8. Fields\n### 8.1 Ceny\nPRICE_FORMULA\n### 8.2 Sklad\nSTOCK_VALUE\n"
                        "### 8.3 Hmotnosť\n\n**Zákaznícka hmotnosť** iba doložená pre daný variant.\n\n"
                        "**Dopravná hmotnosť** SHIPPING_ESTIMATE\n### 8.4 Stav\nACTIVE_DEFAULT\n")
        with reviewed([xml, weight]):
            selected, audit = compiler.compile_instructions([xml, weight], category(), "biketrek")
        result = "\n".join(r["text"] for r in selected)
        for unwanted in ("UNSUPPORTED_XML_MAP", "FICTITIOUS_PRODUCT", "PRICE_FORMULA", "STOCK_VALUE", "SHIPPING_ESTIMATE", "ACTIVE_DEFAULT"):
            self.assertNotIn(unwanted, result)
        self.assertIn("krátky popis, názov a SEO polia sú čistý text", result)
        self.assertIn("iba doložená pre daný variant", result)
        self.assertIn("bez CDATA a bez zástupných značiek", result)
        self.assertEqual(audit["category_profile"], "inner_tubes")

    def test_unknown_revision_and_operator_edits_are_preserved_even_in_omitted_chapter(self):
        original = source("01-9", "## 9. Obrázky a súbory\nGallery workflow\n")
        changed = {**original, "text": original["text"] + "Nové dôležité pravidlo popisu."}
        custom_revision = source("01-9", original["text"].split("\n", 1)[1], revision="1.1 rev15")
        with reviewed([original]):
            for row in (changed, custom_revision):
                selected, audit = compiler.compile_instructions([row], category(), "biketrek")
                self.assertEqual(selected, [row])
                self.assertEqual(audit["omitted_characters"], 0)
            selected, audit = compiler.compile_instructions([original], category(), "biketrek")
            self.assertNotIn(original, selected)
            self.assertEqual(audit["entries"][0]["reasons"], ["unsupported_gallery_workflow"])

    def test_source_and_frozen_request_are_immutable_and_audit_not_sent_to_model(self):
        history = source("01-15", "## 15. History\nSuperseded rule", kind="history")
        original = copy.deepcopy(history)
        with reviewed([history]):
            resolved = rules.resolve(RuleBook(rules=[Rule(id="history", name="History", instructions=history["text"])],
                categories=[CategoryProfile(id="inner_tubes", name="Duše")]), Scope(shop="biketrek", category="inner_tubes"))
        frozen = {"model": "gpt-5.6-sol", "shop": "biketrek", "options": {"language": "sk"},
                  "facts": [], "research": "feed_only", "resolved": resolved}
        with patch.object(compiler, "_select", side_effect=AssertionError("must not recompile an old job")):
            body = provider.request_body(frozen)
        self.assertNotIn("composition", json.loads(body["input"]))
        self.assertNotIn("source_sha256", body["input"])
        self.assertEqual(history, original)
        self.assertEqual(resolved["composition"]["entries"][0]["source_sha256"], hashlib.sha256(original["text"].encode()).hexdigest())

    def test_full_category_faq_matrix_and_guide_are_preserved_verbatim(self):
        text = "### 7.7 Texty\nFAQ otázka\n<guide>https://www.biketrek.sk/a/ako-vybrat-dusu-na-bicykel</guide>\nETRTO pair 23-520\n"
        tube = source("03-7-7", text)
        gallery = source("03-7-9", "### 7.9 Fotografie\nGALLERY_WORKFLOW\n")
        volume = source("03-5-1", "### 5.1 Objem\nBAG_ONLY_VOLUME\n")
        clothing = source("03-5-3", "### 5.3 Oblečenie\nCLOTHING_REGISTER\n")
        with reviewed([tube, gallery, volume, clothing]):
            selected, audit = compiler.compile_instructions([tube, gallery, volume, clothing], category(), "biketrek")
        self.assertIn(tube, selected)
        combined = "\n".join(row["text"] for row in selected)
        for unnecessary in ("GALLERY_WORKFLOW", "BAG_ONLY_VOLUME", "CLOTHING_REGISTER"):
            self.assertNotIn(unnecessary, combined)
        self.assertIn("priamo z nej úplnú HTML tabuľku", combined)
        self.assertIn("API nemá samostatný generátor", combined)
        self.assertEqual(audit["omitted_rules"], 3)

    def test_shared_parameter_rules_follow_registry_without_leaking_tube_contract(self):
        volume = source("03-5-1", "### 5.1 Objem\nVolume is useful for this category.\n")
        with reviewed([volume]):
            selected, _ = compiler.compile_instructions([volume], category("backpacks", "Objem"), "biketrek")
        self.assertIn(volume, selected)
        self.assertNotIn("kompatibilitnú maticu", " ".join(r["text"] for r in selected))

    def test_parameter_table_keeps_universal_formats_and_matching_rows(self):
        formats = source("03-3", "## 3. Formáty hodnôt\n| Parameter | Hodnota | Pravidlo |\n|---|---|---|\n"
                         "| Objem | `20 L` | litres |\n| Rozmer ETRTO | `28-622` | exact pair |\n"
                         "| Áno/Nie parameter | `Áno` / `Nie` | boolean |\n\nNever invent missing measurements.\n")
        with reviewed([formats]):
            selected, _ = compiler.compile_instructions([formats], category("inner_tubes", "Rozmer ETRTO"), "biketrek")
        text = selected[0]["text"]
        self.assertIn("| Rozmer ETRTO |", text)
        self.assertIn("| Áno/Nie parameter |", text)
        self.assertIn("Never invent", text)
        self.assertNotIn("| Objem |", text)

    def test_workflow_sections_preserve_editorial_and_record_requirements(self):
        procedure = source("01-3", "## 3. Workflow\n### 3.1 Prepínače\nCHAT_SWITCH\n"
            "### 3.2 Čo mení ZRYCHLENE\nEDITORIAL_QUALITY\n### 3.3 Postup\n"
            "1. DUPLICATE_SEARCH\n6. **Vytvor obsah individuálne** REVIEW_ACTUAL_FACTS\n7. GALLERY_STEP\n"
            "### 3.4 Pracovný záznam a stavy\n\nRECORD_FACT_SCOPE\n\nStav uvádzaj pravdivo: XML_DELIVERY\n")
        with reviewed([procedure]):
            selected, _ = compiler.compile_instructions([procedure], category(), "biketrek")
        text = selected[0]["text"]
        for expected in ("EDITORIAL_QUALITY", "REVIEW_ACTUAL_FACTS", "RECORD_FACT_SCOPE"):
            self.assertIn(expected, text)
        for omitted in ("CHAT_SWITCH", "DUPLICATE_SEARCH", "GALLERY_STEP", "XML_DELIVERY"):
            self.assertNotIn(omitted, text)

    def test_legacy_digest_requires_all_current_content_sources_and_keeps_api_semantics(self):
        legacy = {"id": "common", "name": "Common", "text": "OLD_DIGEST"}
        modules = [source(key, "CONTENT_" + key) for key in ("01-1", "01-2", "01-4", "01-5", "01-6", "01-7", "03-2", "03-3", "03-4")]
        with reviewed(modules), patch.object(compiler, "_LEGACY_COMMON", hashlib.sha256(legacy["text"].encode()).hexdigest()):
            selected, _ = compiler.compile_instructions([legacy, *modules[:-1]], category(), "biketrek")
            self.assertIn(legacy, selected, "Incomplete source coverage must not remove the digest")
            selected, audit = compiler.compile_instructions([legacy, *modules], category(), "biketrek")
        self.assertNotIn(legacy, selected)
        self.assertEqual(audit["entries"][0]["reasons"], ["superseded_digest"])
        runtime = selected[-1]["text"]
        for expected in ("product_id=null aj pri samostatnom produkte", "výhradne dodané facts", "bezpečnostný návod", "najprv ju otvor"):
            self.assertIn(expected, runtime)

    def test_other_shop_and_split_source_chunks_remain_unchanged(self):
        row = source("01-13", "## 13. Odovzdanie\nFile handover\n")
        split = {**row, "text": row["text"].replace("\n", "\nČasť 1/2\n", 1)}
        with reviewed([row]):
            selected, audit = compiler.compile_instructions([row], category(), "xtrek")
            self.assertEqual(selected, [row])
            self.assertEqual(audit["added_characters"], 0)
        with reviewed([split]):
            selected, _ = compiler.compile_instructions([split], category(), "biketrek")
        self.assertEqual(selected, [split])

    def test_legacy_pending_classification_can_resolve_pinned_uncompiled_rules(self):
        row = source("01-13", "## 13. Odovzdanie\nHistorical job's full original instructions\n")
        book = RuleBook(rules=[Rule(id="delivery", name="Delivery", instructions=row["text"])],
                        categories=[CategoryProfile(id="inner_tubes", name="Duše")])
        with reviewed([row]):
            current = rules.resolve(book, Scope(shop="biketrek", category="inner_tubes"))
            historical = rules.resolve(book, Scope(shop="biketrek", category="inner_tubes"), compile_content=False)
        self.assertEqual(historical["instructions"], [{"id": "delivery", "name": "Delivery", "text": row["text"]}])
        self.assertNotIn("composition", historical)
        self.assertIn("composition", current)

    def test_size_audit_accounts_for_added_contract_without_negative_omission(self):
        row = source("03-7-7", "### 7.7 Texty\nUseful category text\n")
        with reviewed([row]):
            selected, audit = compiler.compile_instructions([row], category(), "biketrek")
        self.assertEqual(audit["omitted_characters"], 0)
        self.assertEqual(audit["selected_characters"], sum(len(r["text"]) for r in selected))
        self.assertEqual(audit["source_characters"] + audit["added_characters"] - audit["omitted_characters"], audit["selected_characters"])


if __name__ == "__main__":
    unittest.main()
