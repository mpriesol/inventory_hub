"""Behavioral AI contract tests: synthetic facts, no paid calls or stock writes."""
import copy
import json
import unittest
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from catalog_fixtures import FakeUpgates, product
from inventory_hub.ai_content_types import CategoryProfile, Content, ParameterDefinition, Policy, Rule, RuleBook, Scope
from inventory_hub.catalog_types import ShopImportOptions
from inventory_hub.routers.ai_content import access
from inventory_hub.services import ai_content as service, ai_content_provider as provider, ai_content_rules as rules, ai_content_worker as worker, catalog_import as imports
from inventory_hub.services.ai_content_validation import overlay, validate_content


def context(products=None, **changes):
    products = products or [product()]
    products[0].description = "Červená prilba s nastaviteľným upínaním."
    value = {"model": "gpt-5.6-sol", "shop": "biketrek", "research": "feed_only", "options": {"language": "sk"},
             "facts": service.facts(products), "resolved": rules.resolve(rules.initial_book(), Scope(category="general"))}
    return {**value, **changes}


def content(**changes):
    data = dict(title="TEST – červená prilba", short_description="Červená prilba | Nastaviteľné upínanie",
        long_description="<h2>Červená prilba</h2><p>Nastaviteľné upínanie.</p>",
        seo_title="Červená prilba TEST s nastaviteľným upínaním", meta_description="Červená prilba TEST s nastaviteľným upínaním.",
        h1_descriptor="Prilba", future_name="TEST", h1_descr_suffix="", parameters=[],
        evidence=[{"claim": "Nastaviteľné upínanie", "source": "feed:1", "quote": "nastaviteľným upínaním"}],
        warnings=[], missing_facts=[])
    return Content.model_validate({**data, **changes})


class RuleTests(unittest.TestCase):
    def test_scope_inheritance_and_provenance(self):
        book = rules.initial_book()
        book.rules.extend([
            Rule(id="supplier_override", name="Supplier", scope=Scope(supplier="test"), policy=Policy(active_after_import=True)),
            Rule(id="shop_override", name="Shop", scope=Scope(shop="xtrek"), policy=Policy(review_required=False)),
            Rule(id="product_override", name="Product", scope=Scope(product="PL-A-001"), policy=Policy(active_after_import=False)),
        ])
        result = rules.resolve(book, Scope(shop="xtrek", supplier="test", product="PL-A-001"), Policy(confirm_import=False))
        self.assertEqual(result["policy"], dict(review_required=False, active_after_import=False, show_cost_estimate=True, confirm_import=False))
        self.assertEqual(result["origins"]["active_after_import"], "Product")
        self.assertEqual(result["origins"]["confirm_import"], "run")
        self.assertTrue(rules.resolve(book, Scope(shop="biketrek"))["policy"]["review_required"])

    def test_conflicts_fail_instead_of_depending_on_rule_id(self):
        book = rules.initial_book()
        book.rules += [Rule(id="a", name="a", scope=Scope(shop="xtrek", supplier="test"), policy=Policy(confirm_import=True)),
                       Rule(id="b", name="b", scope=Scope(shop="xtrek", brand="TEST"), policy=Policy(confirm_import=False))]
        with self.assertRaisesRegex(imports.CatalogError, "Conflicting"):
            rules.resolve(book, Scope(shop="xtrek", supplier="test", brand="TEST"))

    def test_tube_rules_are_category_scoped_and_not_auto_ready(self):
        book = rules.initial_book()
        generic = rules.resolve(book, Scope(category="general"))
        tubes = rules.resolve(book, Scope(category="inner_tubes"))
        self.assertFalse(any("ETRTO" in r["text"] for r in generic["instructions"]))
        self.assertFalse(tubes["category"]["automatic_import_ready"])
        self.assertGreater(len(tubes["category"]["parameters"]), 5)

    def test_invalid_domains_duplicate_parameters_and_ambiguous_scopes_rejected(self):
        for value in ("http://northfinder.com", "northfinder.com?token=x", "localhost", "127.0.0.1"):
            with self.assertRaises(ValueError):
                Rule(id="test", name="test", official_domains=[value])
        with self.assertRaises(ValueError):
            CategoryProfile(id="test", name="test", parameters=[ParameterDefinition(name="Farba"), ParameterDefinition(name="farba")])
        self.assertEqual(len(RuleBook(rules=[Rule(id="a", name="a"), Rule(id="b", name="b")]).rules), 2)


class ContentTests(unittest.TestCase):
    def test_product_text_normalizes_dashes_and_keeps_source_evidence_intact(self):
        fields = ("title", "short_description", "long_description", "seo_title", "meta_description",
                  "h1_descriptor", "future_name", "h1_descr_suffix")
        text = "Model ‐ ‑ ‒ – — ― ⸺ ⸻ ﹘ ﹣ － test &ndash; &mdash; &#8212; &#x2013; &hyphen;"
        evidence = [{"claim": "Model – vlastnosť", "source": "https://manufacturer.example/model–a", "quote": "Model\u2011A"}]
        parameters = [{"name": "Rozsah", "values": ["1–2"], "product_id": None}]
        value = content(**dict.fromkeys(fields, text), evidence=evidence, parameters=parameters)
        for field in fields:
            self.assertEqual(getattr(value, field), "Model - - - - - - - - - - - test - - - - -")
        self.assertEqual([e.model_dump() for e in value.evidence], evidence)
        self.assertEqual([p.model_dump() for p in value.parameters], parameters)
        html = content(long_description='<p>Model — popis &amp; &lt;detail&gt;: −5 °C.</p>')
        self.assertEqual(html.long_description, '<p>Model - popis &amp; &lt;detail&gt;: −5 °C.</p>')
        self.assertEqual(Content.model_validate(value.model_dump()), value)

    def test_good_content_and_empty_registry(self):
        self.assertEqual(validate_content(content(), context())["errors"], [])
        bad = content(parameters=[{"name": "Invented", "values": ["yes"], "product_id": None}])
        self.assertIn("ai_unregistered_parameter:Invented", validate_content(bad, context())["errors"])

    def test_required_values_and_scope(self):
        ctx = context()
        ctx["resolved"]["category"]["parameters"] = [ParameterDefinition(name="Farba", required=True, values=["červená"]).model_dump()]
        self.assertIn("ai_required_parameter:Farba", validate_content(content(), ctx)["errors"])
        invalid = content(parameters=[{"name": "Farba", "values": ["modrá"], "product_id": 1}])
        errors = validate_content(invalid, ctx)["errors"]
        self.assertIn("ai_parameter_scope:Farba", errors)
        self.assertIn("ai_parameter_value:Farba", errors)

    def test_ordinary_html_formatting_is_accepted_without_rewriting(self):
        descriptions = (
            '<div class="specs" id="sizes"><h1>Rozmery</h1><span style="font-weight: bold; color: #333">26 × 2.00</span></div>',
            '<table style="width:100%"><caption>Rozmery</caption><thead><tr><th scope="col" colspan="2">ETRTO</th></tr></thead>'
            '<tbody><tr><td rowspan="2">50-559</td><td data-unit="mm">50</td></tr></tbody></table>',
            '<p><a href="https://manufacturer.example/manual?lang=sk&amp;version=1" target="_blank" rel="noopener">Návod</a></p>'
            '<img src="https://manufacturer.example/product.jpg" alt="Duša" width="200" loading="lazy">',
            '<section aria-label="Použitie"><details open><summary>Kompatibilita</summary><p>26 × 2.00, −5 °C &amp; viac.</p></details></section>',
            '<p style="background-image: url(https://manufacturer.example/background.png); font-family: \'Arial\'; scroll-behavior: smooth">Text</p>',
            '<!DOCTYPE html><!-- Static product description --><p>Rozmer 26 × 2.00, ventil 40 mm.</p>',
            '<a href="https://manufacturer.example/manual" href="javascript:ignored()">Návod</a>',
        )
        for description in descriptions:
            with self.subTest(description=description):
                value = content(long_description=description)
                self.assertEqual(validate_content(value, context())["errors"], [])
                self.assertEqual(value.long_description, description)
                item = overlay(imports.build_item([product()], ShopImportOptions(), {}, True),
                    {"active_after_import": False, "content": value.model_dump()}, "sk")
                self.assertEqual(item.payload["descriptions"][0]["long_description"], description)

    def test_active_code_embeds_and_executable_urls_still_block(self):
        for description in (
            '<script>alert(1)</script>', '<p ONCLICK="x()">Text</p>', '<img src="x" onerror="x()">',
            '<iframe src="https://example.test"></iframe>', '<object data="https://example.test"></object>',
            '<embed src="https://example.test">', '<svg><animate attributeName="href" values="javascript:x()"/></svg>',
            '<math><annotation-xml encoding="text/html">Text</annotation-xml></math>',
            '<meta http-equiv="refresh" content="0;url=https://example.test">', '<base href="https://example.test">',
            '<link rel="stylesheet" href="https://example.test/a.css">', '<style>@import "https://example.test/a.css";</style>',
            '<div srcdoc="&lt;script&gt;x()&lt;/script&gt;">Text</div>',
            '<a href="javascript:alert(1)">Text</a>', '<a href="&#106;ava&#x73;cript:alert(1)">Text</a>',
            '<a href="java&#10;script:alert(1)">Text</a>', '<a href=" \tJaVaScRiPt:alert(1)">Text</a>',
            '<a href="vbscript:msgbox(1)">Text</a>', '<a href="data:text/html,&lt;script&gt;x()&lt;/script&gt;">Text</a>',
            '<a href="javascript:x()" href="https://example.test">Text</a>',
            '<img srcset="https://example.test/a.png 1x, data:image/svg+xml,test 2x">',
            '<form action="javascript:x()">Text</form>', '<button formaction="javascript:x()">Text</button>',
            '<p style="width: expression(alert(1))">Text</p>', '<p style="background:url(javascript:x())">Text</p>',
            '<p style="behavior:url(test.htc)">Text</p>', '<p style="-moz-binding:url(test.xml)">Text</p>',
            r'<p style="width: e\78pression(alert(1))">Text</p>',
            '<p style="width: exp/**/ression(alert(1))">Text</p>',
            r'<p style="background:url(\6a avascript:x())">Text</p>',
            '<!--><script>alert(1)</script>-->',
            '<!---><script>alert(1)</script>-->',
            '<noscript><p title="</noscript><img src=x onerror=alert(1)>">',
            '<![CDATA[><img src=x onerror=alert(1)>]]>',
        ):
            with self.subTest(description=description):
                self.assertIn("ai_unsafe_html", validate_content(content(long_description=description), context())["errors"])

    def test_contacts_plain_text_and_unverified_sources_still_block(self):
        self.assertIn("ai_manufacturer_contact", validate_content(content(long_description='<p>a@example.com</p>'), context())["errors"])
        self.assertIn("ai_plain_text_required:title", validate_content(content(title='<b>Prilba</b>'), context())["errors"])
        bad = content(evidence=[{"claim": "waterproof", "quote": "not in feed", "source": "feed:1"}])
        self.assertIn("ai_unverified_feed_evidence", validate_content(bad, context())["errors"])

    def test_official_evidence_requires_exact_opened_page_without_domain_setup(self):
        ctx = context()
        ctx["resolved"]["official_domains"] = ["northfinder.com"]
        source = "https://northfinder.com/product"
        value = content(evidence=[{"claim": "fact", "quote": "source quote", "source": source}])
        self.assertIn("ai_unverified_official_evidence", validate_content(value, ctx)["errors"])
        self.assertNotIn("ai_unverified_official_evidence", validate_content(value, ctx, [source])["errors"])
        for url in ("https://northfinder.com.evil.test/product", "https://evilnorthfinder.com/product"):
            value.evidence[0].source = url
            self.assertIn("ai_unverified_official_evidence", validate_content(value, ctx, [source])["errors"])
        discovered = "https://manufacturer.example/product"
        value.evidence[0].source = discovered
        self.assertNotIn("ai_unverified_official_evidence", validate_content(value, ctx, [discovered])["errors"])
        ctx["resolved"]["official_domains"] = []
        self.assertNotIn("ai_unverified_official_evidence", validate_content(value, ctx, [discovered])["errors"])
        for url in ("http://manufacturer.example/product", "https://user:pass@manufacturer.example/product", "https://manufacturer.example/product?token=private", "https:///product"):
            value.evidence[0].source = url
            self.assertIn("ai_unverified_official_evidence", validate_content(value, ctx, [url])["errors"])

    def test_feed_evidence_matches_real_multiline_text_and_html(self):
        ctx = context()
        ctx["facts"][0]["description"] = (
            '<p>Oceľová základňa\r\n• Hadica 1000\u00a0mm</p>'
            '<p>• Hliníková konštrukcia "Air\'s"</p>')
        quote = 'Oceľová základňa\n• Hadica 1000 mm\n• Hliníková konštrukcia "Air\'s"'
        value = content(evidence=[{"claim": "Konštrukcia pumpy", "source": "feed:1", "quote": quote}])
        self.assertEqual(validate_content(value, ctx)["errors"], [])
        value.evidence[0].quote = quote.replace("1000", "2000")
        self.assertIn("ai_unverified_feed_evidence", validate_content(value, ctx)["errors"])

    def test_feed_evidence_accepts_typographic_hyphens_without_changing_facts(self):
        ctx = context()
        ctx["facts"][0]["description"] = '<p>Model\u2011A, rozmer 50\u2010559, hmotnosť 980 g.</p>'
        for quote in (
            'Model-A, rozmer 50-559, hmotnosť 980 g.',
            'Model\u2010A, rozmer 50\u2011559, hmotnosť 980 g.',
        ):
            with self.subTest(quote=quote):
                value = content(evidence=[{"claim": "Údaj", "source": "feed:1", "quote": quote}])
                self.assertEqual(validate_content(value, ctx)["errors"], [])
                self.assertEqual(value.evidence[0].quote, quote, "Comparison must not rewrite the saved quote")
        for quote in (
            'Model-A, rozmer 50-559, hmotnosť 890 g.',
            'Model-A, rozmer 50-559, hmotnosť 980 kg.',
            'Model-A, rozmer 50-559, hmotnosť 980 g. Reflexné bočnice.',
            'Model-A, hmotnosť 980 g.',
            'Model-A, rozmer 50\u2212559',
            'Model-A, rozmer 50\u2013559',
            'Model-A, rozmer 50\u2014559',
        ):
            with self.subTest(quote=quote):
                value = content(evidence=[{"claim": "Údaj", "source": "feed:1", "quote": quote}])
                self.assertIn("ai_unverified_feed_evidence", validate_content(value, ctx)["errors"])

    def test_legacy_official_url_is_normalized_but_still_requires_exact_opening(self):
        source = "https://manufacturer.example/product"
        value = content(evidence=[{"claim": "fact", "quote": "source quote", "source": "official:" + source}])
        self.assertEqual(value.evidence[0].source, source)
        self.assertEqual(validate_content(value, context(), [source])["errors"], [])
        for opened in ([], [source + "-other"], ["official:" + source]):
            checks = validate_content(value, context(), opened)
            self.assertIn("ai_unverified_official_evidence", checks["errors"])
            self.assertEqual(checks["evidence_errors"], [{"index": 0, "source": source, "reason": "not_opened"}])

    def test_source_diagnostics_do_not_guess_urls_or_interrupt_validation(self):
        for source, reason in (
            ("official:manufacturer.example Product name", "invalid_https_url"),
            ("https://[broken", "invalid_https_url"),
            ("https://manufacturer.example:bad/product", "invalid_https_url"),
            ("official:http://manufacturer.example/product", "invalid_https_url"),
            ("official:https://user:pass@manufacturer.example/product", "invalid_https_url"),
            ("official:https://manufacturer.example/product?token=private", "query_not_allowed"),
        ):
            with self.subTest(source=source):
                value = content(evidence=[{"claim": "fact", "quote": "source quote", "source": source}])
                checks = validate_content(value, context(), [value.evidence[0].source])
                self.assertIn("ai_unverified_official_evidence", checks["errors"])
                self.assertFalse(checks["automatic_ready"])
                self.assertEqual(checks["evidence_errors"][0]["reason"], reason)

    def test_feed_diagnostic_identifies_only_the_failed_evidence_row(self):
        value = content()
        value.evidence.append(value.evidence[0].model_copy(update={"source": "feed:999"}))
        checks = validate_content(value, context())
        self.assertEqual(checks["evidence_errors"], [{"index": 1, "source": "feed:999", "reason": "feed_quote_not_found"}])

    def test_labelled_feed_parameters_can_share_a_quote_without_changing_facts(self):
        ctx = context()
        ctx["facts"][0]["parameters"] = [
            {"name": "Veľkosť plášťa", "value": '19"'},
            {"name": "Ventil duše", "value": "AV - autoventil"},
        ]
        for quote, source, valid in [
            ('Veľkosť plášťa: 19"; Ventil duše: AV - autoventil', 'feed:1', True),
            ('Veľkosť plášťa: 19"\nVentil duše: AV - autoventil', 'feed:1', True),
            ('Veľkosť plášťa: 19"; Ventil duše: AV\n- autoventil', 'feed:1', True),
            ('Ventil duše: AV - autoventil', 'feed:1', True),
            ('Veľkosť plášťa: 29"; Ventil duše: AV - autoventil', 'feed:1', False),
            ('Veľkosť plášťa: AV - autoventil; Ventil duše: 19"', 'feed:1', False),
            ('Veľkosť plášťa: 19"; Ventil duše: AV - autoventil', 'feed:999', False),
            ('Veľkosť plášťa: 19"; Ventil duše: AV - autoventil; Nepriestrelná duša', 'feed:1', False),
        ]:
            with self.subTest(quote=quote, source=source):
                value = content(evidence=[{"claim": "Rozmer a ventil", "source": source, "quote": quote}])
                checks = validate_content(value, ctx)
                self.assertEqual('ai_unverified_feed_evidence' not in checks['errors'], valid)
                self.assertEqual(value.evidence[0].quote, quote)

    def test_human_approval_accepts_editorial_concerns_but_keeps_technical_checks(self):
        ctx = context(approval='human')
        value = content(missing_facts=['Rozpor v dĺžke ventilu'], evidence=[
            {'claim': 'Ventil', 'source': 'feed:1', 'quote': 'Nedoložený citát'},
            {'claim': 'Tabuľka', 'source': 'https://manufacturer.example/table.pdf', 'quote': 'AV 33 mm'},
        ])
        original = value.model_dump()
        expected = ['ai_missing_facts', 'ai_unverified_feed_evidence', 'ai_unverified_official_evidence']
        strict = validate_content(value, ctx)
        self.assertEqual(strict['errors'], expected, 'Context alone must not approve a newly edited draft')
        accepted = validate_content(value, ctx, human_approved=True)
        self.assertEqual(accepted['errors'], [])
        self.assertEqual(accepted['manual_overrides'], expected)
        self.assertTrue(set(expected) <= set(accepted['warnings']))
        self.assertEqual(accepted['evidence_errors'], strict['evidence_errors'])
        self.assertFalse(accepted['automatic_ready'])
        self.assertEqual(value.model_dump(), original)
        value.long_description = '<p onclick="run()">Popis</p>'
        value.parameters = content(parameters=[{'name': 'Neznámy', 'values': ['x'], 'product_id': None}]).parameters
        blocked = validate_content(value, ctx, human_approved=True)
        self.assertEqual(blocked['errors'], ['ai_unregistered_parameter:Neznámy', 'ai_unsafe_html'])

    def test_feed_evidence_checks_source_fields_without_serialization_artifacts(self):
        ctx = context()
        ctx["facts"][0].update(description="Oceľová základňa", brand="Značka", parameters=[{"name": "Tlak", "value": "4 bar"}])
        for quote, source, valid in [
            ("4 bar", "feed:1", True),
            ("4 bar", "feed:999", False),
            ("Oceľová základňa Značka", "feed:1", False),
            ("'name': 'Tlak'", "feed:1", False),
            ("<p> </p>", "feed:1", False),
        ]:
            with self.subTest(quote=quote, source=source):
                value = content(evidence=[{"claim": "Údaj", "source": source, "quote": quote}])
                self.assertEqual("ai_unverified_feed_evidence" not in validate_content(value, ctx)["errors"], valid)

    def test_content_overlay_preserves_money_identity_and_source(self):
        p = product()
        source = p.model_dump()
        item = imports.build_item([p], ShopImportOptions(), {}, True, {1: Decimal("99")})
        original = copy.deepcopy(item.payload)
        legacy = {**content().model_dump(), "title": "TEST — prilba", "long_description": "<p>Model &ndash; popis.</p>"}
        enriched = overlay(item, {"active_after_import": True, "content": legacy, "registered_parameters": False, "safety": "Noste prilbu správne."}, "sk")
        for key in ("code", "ean", "prices", "images", "parameters", "manufacturer"):
            self.assertEqual(enriched.payload.get(key), original.get(key))
        self.assertEqual(p.model_dump(), source)
        self.assertEqual(enriched.payload["descriptions"][0]["title"], "TEST - prilba")
        self.assertIn("<p>Model - popis.</p>", enriched.payload["descriptions"][0]["long_description"])
        self.assertEqual(legacy["title"], "TEST — prilba", "Overlay must not modify the stored source dictionary")
        imports._assert_payload(enriched.payload, expected_active=True)
        with self.assertRaises(imports.CatalogError):
            imports._assert_payload(enriched.payload)
        self.assertIn("Bezpečnostné", enriched.payload["descriptions"][0]["long_description"])
        self.assertEqual(next(m["value"] for m in enriched.payload["metas"] if m["key"] == "validation_required"), "0")

    def test_readback_checks_real_content_and_visibility_without_extra_get(self):
        p = product()
        item = overlay(imports.build_item([p], ShopImportOptions(), {}, True),
            {"active_after_import": True, "content": content().model_dump()}, "sk")
        client = FakeUpgates()
        client.post("products", {"products": [item.payload]})
        self.assertIsNotNone(imports._verify_created(client, item.model_dump(mode="json")))
        client.products[item.code]["descriptions"][0]["seo_description"] = ""
        with self.assertRaisesRegex(imports.CatalogError, "seo_description"):
            imports._verify_created(client, item.model_dump(mode="json"))

    def test_stock_is_forbidden_even_for_active_approved_content(self):
        for field in ("stocks", "stock", "stock_increment", "stock_position", "variants_stock"):
            with self.assertRaises(imports.CatalogError):
                imports._assert_payload({"active_yn": True, "variants": [{field: 1}]}, expected_active=True)

    def test_text_metadata_uses_upgates_language_values_shape(self):
        from inventory_hub.services.ai_content_upgates import meta_value, verify_content
        item = overlay(imports.build_item([product()], ShopImportOptions(), {}, True),
            {"active_after_import": False, "content": content().model_dump(), "supplier_name": "Fixture supplier"}, "sk")
        fields = {m["key"]: m for m in item.payload["metas"]}
        self.assertEqual(fields["h1_descriptor"], {"key": "h1_descriptor", "values": [{"language": "sk", "value": "Prilba"}]})
        self.assertEqual(fields["future_name"], {"key": "future_name", "value": "TEST"})
        self.assertEqual(meta_value(fields["supplier_name"], "sk"), "Fixture supplier")
        remote = copy.deepcopy(item.payload)
        for m in remote["metas"]:
            m["type"] = "textarea" if m["key"] == "h1_descriptor" else "input"
        verify_content(remote, item.payload)
        next(m for m in remote["metas"] if m["key"] == "h1_descriptor")["values"] = [{"language": "en", "value": "Prilba"}]
        with self.assertRaises(imports.CatalogError):
            verify_content(remote, item.payload)

    def test_metadata_respects_existing_shop_language_configuration(self):
        from inventory_hub.services.ai_content_upgates import verify_content
        item = overlay(imports.build_item([product()], ShopImportOptions(), {}, True),
            {"active_after_import": False, "content": content().model_dump(),
             "meta_common": {"h1_descriptor": True, "future_name": False}}, "sk")
        fields = {m["key"]: m for m in item.payload["metas"]}
        self.assertEqual(fields["h1_descriptor"]["value"], "Prilba")
        self.assertEqual(fields["future_name"]["values"], [{"language": "sk", "value": "TEST"}])
        remote = copy.deepcopy(item.payload)
        verify_content(remote, item.payload)
        next(m for m in remote["metas"] if m["key"] == "h1_descriptor")["value"] = "Wrong"
        with self.assertRaises(imports.CatalogError):
            verify_content(remote, item.payload)

    def test_facts_omit_financial_stock_and_manufacturer_contact_data(self):
        p = product(); p.manufacturer_description = "Private manufacturer contact"
        exported = service.facts([p])[0]
        self.assertFalse({"prices", "supplier_stock", "manufacturer_description", "source_hash", "url"}.intersection(exported))

    def test_price_refresh_does_not_invalidate_content_but_changed_facts_do(self):
        p = product()
        before = service.source_digest([p]); p.prices.retail_gross = Decimal("200")
        self.assertEqual(before, service.source_digest([p]))
        p.description = "Different model facts"
        self.assertNotEqual(before, service.source_digest([p]))


class WorkerErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_safe_diagnostic_reaches_history_without_changing_retry_or_budget_rules(self):
        @asynccontextmanager
        async def session():
            yield None
        for status, expected_state, reserved in ((400, "failed", 0), (503, "uncertain", 1)):
            job = SimpleNamespace(status="queued", context=context(), kind="product", revision=1,
                                  events=[], actual_usd=None, reserved_usd=1)
            error = provider.ProviderError(httpx.Response(status, json={"error": {"code": "model_not_found"}}))
            with patch.object(worker, "get_session_context", session), \
                 patch.object(service, "get_job", AsyncMock(return_value=job)), \
                 patch.object(provider, "generate", AsyncMock(side_effect=error)) as generate, \
                 patch.object(provider.settings, "AI_CONTENT_ENABLED", True):
                await worker.generation("synthetic-job")
            generate.assert_awaited_once()
            self.assertEqual((job.status, job.reserved_usd), (expected_state, reserved))
            self.assertIn(f"OpenAI HTTP {status}; code=model_not_found", job.events[-1]["note"])


class ProviderTests(unittest.TestCase):
    def test_provider_output_normalizes_product_dashes_before_storage(self):
        raw = {**content().model_dump(), "title": "TEST — prilba", "long_description": "<p>Model &mdash; popis.</p>"}
        response = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(raw)}]}]}
        output, _ = provider.parse_response(response)
        self.assertEqual(output["title"], "TEST - prilba")
        self.assertEqual(output["long_description"], "<p>Model - popis.</p>")
        self.assertEqual(output["evidence"], raw["evidence"])

    def test_provider_contract_and_parsing_use_canonical_evidence_urls(self):
        source = "https://manufacturer.example/product"
        raw = content().model_dump()
        raw["evidence"] = [{"claim": "fact", "quote": "source quote", "source": "official:" + source}]
        response = {"status": "completed", "output": [
            {"type": "web_search_call", "action": {"type": "open_page", "url": source}},
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(raw)}]},
        ]}
        parsed, opened = provider.parse_response(response)
        self.assertEqual(parsed["evidence"][0]["source"], source)
        self.assertEqual(validate_content(Content.model_validate(parsed), context(), opened)["errors"], [])
        body = provider.request_body(context(research="official"))
        self.assertIn("Nepridávaj prefix official:", body["instructions"])
        self.assertIn("exact complete https:// URL", body["text"]["format"]["schema"]["$defs"]["Evidence"]["properties"]["source"]["description"])

    def test_provider_diagnostics_exclude_secrets_and_arbitrary_values(self):
        error = provider.ProviderError(httpx.Response(400, json={"error": {
            "code": "unsupported_value", "param": "reasoning.effort", "message": "private input and secret",
        }}))
        self.assertEqual(error.code, "ai_provider_rejected")
        self.assertEqual(str(error), "OpenAI HTTP 400; code=unsupported_value; param=reasoning.effort")
        for body in ({"error": {"code": "private-secret", "param": "private-secret", "message": "private-secret"}},
                     {"error": "private-secret"}, ["private-secret"]):
            self.assertEqual(str(provider.ProviderError(httpx.Response(401, json=body))), "OpenAI HTTP 401")
        error = provider.ProviderError(httpx.Response(503, text="private proxy response"))
        self.assertEqual((error.code, str(error)), ("ai_outcome_unknown", "OpenAI HTTP 503"))

    def test_schema_strict_and_response_contract(self):
        body = provider.request_body(context())
        self.assertFalse(body["store"])
        self.assertNotIn("tools", body)
        self.assertEqual(body["text"]["format"]["schema"]["required"], list(Content.model_fields))
        response = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": content().model_dump_json()}]}]}
        self.assertEqual(provider.parse_response(response)[0]["title"], content().title)
        for state in ("incomplete", "failed"):
            with self.assertRaises(imports.CatalogError):
                provider.parse_response({**response, "status": state})

    def test_refusal_and_unknown_model_fail_closed(self):
        with self.assertRaises(imports.CatalogError):
            provider.parse_response({"status": "completed", "output": [{"content": [{"type": "refusal"}]}]})
        with self.assertRaises(imports.CatalogError):
            provider.estimate(context(model="unpriced-model"))

    def test_official_research_is_required_and_remains_bounded(self):
        ctx = context(research="official")
        ctx["resolved"]["official_domains"] = ["manufacturer.example"]
        body = provider.request_body(ctx)
        self.assertEqual(body["tool_choice"], "required")
        self.assertEqual(body["tools"], [{"type": "web_search"}])
        self.assertEqual(json.loads(body["input"])["preferred_official_domains"], ["manufacturer.example"])
        self.assertEqual(body["max_tool_calls"], 6)
        # The quote covers every allowed search, not the former three-call cap.
        rate = provider.RATES[ctx["model"]]
        prompt_bytes = len(json.dumps(body, ensure_ascii=False).encode()) + 60000
        expected = (Decimal(prompt_bytes) * Decimal(rate["input"]) / 1000000
                    + Decimal(provider.MAX_OUTPUT) * Decimal(rate["output"]) / 1000000
                    + Decimal("0.06")).quantize(Decimal("0.000001"))
        self.assertEqual(provider.estimate(ctx), expected)
        feed_body = provider.request_body(context())
        self.assertNotIn("tool_choice", feed_body)
        self.assertNotIn("tools", feed_body)
        ctx["resolved"]["official_domains"] = []
        self.assertEqual(provider.request_body(ctx)["tool_choice"], "required")

    def test_cost_uses_decimal_and_counts_cached_input_and_search(self):
        usage, cost = provider.usage_cost({"usage": {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 500}, "output_tokens": 100},
            "output": [{"type": "web_search_call"}]}, "gpt-5.6-sol")
        self.assertEqual(cost, Decimal("0.014200"))
        self.assertEqual(usage["web_calls"], 1)

    def test_access_is_denied_when_missing_or_incorrect(self):
        from pydantic import SecretStr
        with patch.object(provider.settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr("fixture-token-only-123456789")):
            for token in (None, "Bearer wrong", "fixture-token-only-123456789", "Basic fixture-token-only-123456789", "Bearer nesprávny"):
                with self.assertRaises(HTTPException):
                    access(token)
            access("Bearer fixture-token-only-123456789")
