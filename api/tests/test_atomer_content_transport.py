"""Whole-family identity, minimal writes and complete content/stock observations."""
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from inventory_hub.services import atomer_content_source as transport


def parent():
    return {"code": "FAMILY", "product_id": 101, "manufacturer": "Brand", "ean": None,
            "variants_exists_yn": True, "set_yn": False, "active_yn": True, "stock": Decimal("3"),
            "availability": "Available", "can_add_to_basket_yn": True,
            "last_update_time": "2026-01-01 10:00:00", "creation_time": "2025-01-01 10:00:00",
            "descriptions": [
                {"language": "sk", "title": "Family", "long_description": "<p>Old</p>", "seo_title": None,
                 "seo_description": "Old SEO", "short_description": "Keep", "url": "https://shop.test/family"},
                {"language": "en", "title": "Family EN", "long_description": "<p>Keep EN</p>"}],
            "images": [{"url": "https://shop.test/image.jpg"}], "prices": [{"price_original": "23.50"}],
            "variants": [
                {"code": "F-A", "product_code": "FAMILY", "product_id": 101, "variant_id": 201,
                 "ean": "5901234123457", "stock": 2, "availability": "Available", "active_yn": True},
                {"code": "F-B", "product_code": "FAMILY", "product_id": 101, "variant_id": 202,
                 "ean": "4006381333931", "stock": 1, "availability": "Available", "active_yn": True}]}


def target():
    return {"code": "F-A", "parent_code": "FAMILY", "variant_code": "F-A", "product_id": 101, "variant_id": 201}


def response(row):
    return {"current_page": 1, "current_page_items": 1, "number_of_items": 1,
            "number_of_pages": 1, "products": [row]}


def remote(row=None, identity=None):
    with patch.object(transport.source, "_request", return_value=response(row or parent())):
        return transport._read(object(), identity or target())


def group(row=None):
    row = row or parent()
    return {"source_code": row["code"], "rows": [
        {"code": variant["code"], "parent_code": row["code"], "ean": variant.get("ean"),
         "manufacturer": row["manufacturer"], "long_description": "<p>New</p>",
         "seo_title": "New title", "seo_description": "New SEO"} for variant in row["variants"]]}


class AtomerParentIdentityTests(TestCase):
    def test_standalone_keeps_its_native_identity_and_never_becomes_a_family(self):
        row = parent(); row.update(code="SINGLE", variants_exists_yn=False, variants=[], ean="0727670929720")
        identity = {"code": "SINGLE", "parent_code": "SINGLE", "variant_code": None,
                    "product_id": 101, "variant_id": None}
        observed = remote(row, identity)
        selected = {"source_code": "SINGLE", "rows": [{"code": "SINGLE", "parent_code": "",
            "manufacturer": "Brand", "ean": "727670929720"}]}
        self.assertEqual(transport.verify_source(selected, observed)[0]["variant_id"], None)
        self.assertEqual(observed["identity"]["ean"], "0727670929720")
        changed = deepcopy(row); changed["variants_exists_yn"] = True
        changed["variants"] = [{"code": "NEW-LEAF", "product_code": "SINGLE", "product_id": 101,
                                "variant_id": 999, "ean": None}]
        with self.assertRaises(transport.source.SourceError):
            remote(changed, observed["identity"])

    def test_full_parent_read_preserves_leaf_anchor_without_weakening_stock_reader(self):
        with patch.object(transport.source, "_request", return_value=response(parent())) as request:
            observed = transport._read(object(), target())
        self.assertEqual(request.call_args.args[1:3], ("get", "products"))
        self.assertEqual(request.call_args.kwargs["params"], {"codes": "FAMILY", "page": 1, "current_page_items": 100})
        self.assertEqual(observed["identity"]["code"], "F-A")
        self.assertEqual(observed["identity"]["variant_id"], 201)
        self.assertEqual([r["variant_id"] for r in observed["identity"]["full_family"]["variants"]], [201, 202])
        self.assertEqual(observed["parent"]["stock"], "3")
        with self.assertRaises(transport.source.SourceError):
            transport.source._observation(response(parent()), {"code": "FAMILY", "parent_code": "FAMILY",
                "variant_code": None, "product_id": 101, "variant_id": None})

    def test_partial_duplicate_or_changed_families_fail_closed(self):
        frozen = remote()["identity"]
        cases = []
        row = parent(); row["variants"] = row["variants"][:1]; cases.append(row)
        row = parent(); row["variants"][1]["variant_id"] = 201; cases.append(row)
        row = parent(); row["variants"][1]["code"] = "f-a"; cases.append(row)
        row = parent(); row["variants"][1]["product_id"] = 999; cases.append(row)
        row = parent(); row["variants"][1]["ean"] = "5901234123457"; cases.append(row)
        row = parent(); row["manufacturer"] = "Other"; cases.append(row)
        row = parent(); row["variants_exists_yn"] = False; cases.append(row)
        row = parent(); row["variants"] = []; cases.append(row)
        row = parent(); row["product_id"] = True; cases.append(row)
        for row in cases:
            with self.subTest(row=row), self.assertRaises(transport.source.SourceError):
                remote(row, frozen)

    def test_duplicate_or_missing_language_is_not_empty_content(self):
        for descriptions in ([{"language": "en"}], [{"language": "sk"}, {"language": "sk"}], None):
            row = parent(); row["descriptions"] = descriptions
            with self.subTest(descriptions=descriptions), self.assertRaises(transport.source.SourceError):
                remote(row)
        row = parent(); del row["descriptions"][0]["seo_title"]
        observed = remote(row)
        self.assertEqual(transport.field_values(observed, ["seo_title"]), {"seo_title": {"missing": True}})
        self.assertNotEqual(transport.field_values(observed, ["seo_title"]), {"seo_title": None})

    def test_source_identity_never_uses_name_and_cannot_hide_ean_or_brand_conflict(self):
        observed = remote()
        self.assertEqual(len(transport.verify_source(group(), observed)), 2)
        for mutate in (
            lambda g: g["rows"][0].update(ean="4006381333931"),
            lambda g: g["rows"][0].update(manufacturer="Other"),
            lambda g: g["rows"][0].update(code="F-B"),
            lambda g: g["rows"].pop(),
            lambda g: g.update(source_code="OTHER"),
            lambda g: g["rows"][0].update(code="unmatched", ean="", name="Family"),
        ):
            selected = group(); mutate(selected)
            with self.subTest(selected=selected), self.assertRaises(transport.source.SourceError):
                transport.verify_source(selected, observed)

    def test_different_source_code_can_use_unique_valid_gtin_preserving_original_zeros(self):
        row = parent(); row["variants"][0]["ean"] = "727670929720"
        selected = group(row); selected["rows"][0]["code"] = "source-alias"
        selected["rows"][0]["ean"] = "0727670929720"
        evidence = transport.verify_source(selected, remote(row))
        self.assertEqual(evidence[0]["matched_by"], "unique_valid_gtin")
        self.assertEqual(evidence[0]["source_ean"], "0727670929720")
        self.assertEqual(evidence[0]["target_code"], "F-A")

    def test_same_code_cannot_override_barcode_pointing_to_a_sibling(self):
        selected = group(); selected["rows"][0]["ean"] = selected["rows"][1]["ean"]
        with self.assertRaisesRegex(transport.source.SourceError, "ean_conflict"):
            transport.verify_source(selected, remote())


class AtomerContentPayloadTests(TestCase):
    def test_only_selected_description_fields_sent_and_protected_values_remain_observed(self):
        observed = remote()
        values = {"long_description": "<p><strong>New</strong></p>", "seo_title": "New title"}
        result = transport.build_patch(values, observed, list(values))
        self.assertEqual(result["payload"], {"products": [{"code": "FAMILY", "descriptions": [{"language": "sk", **values}]}]})
        before = transport.protected_observation(observed, list(values))
        updated = deepcopy(observed)
        updated["parent"]["descriptions"][0].update(values)
        updated["parent"]["last_update_time"] = "2026-11-02 12:00:00"
        self.assertEqual(transport.protected_observation(updated, list(values)), before)
        updated["parent"]["stock"] = "2"
        protected = transport.protected_observation(updated, list(values))
        self.assertEqual(protected["content"], before["content"])
        self.assertNotEqual(protected["operations"], before["operations"])
        updated["parent"]["descriptions"][1]["long_description"] = "Lost translation"
        self.assertNotEqual(transport.protected_observation(updated, list(values))["content"], before["content"])

    def test_missing_versus_null_operational_facts_are_retained(self):
        before = remote(); after = deepcopy(before)
        after["parent"]["variants"][0]["stocks"] = None
        self.assertNotEqual(transport.protected_observation(before)["operations"], transport.protected_observation(after)["operations"])

    def test_html_format_loss_fails_readback_even_if_visible_words_match(self):
        expected = {"long_description": "<p><strong>Important</strong></p>"}
        self.assertFalse(transport.compare_values(expected, {"long_description": "<p>Important</p>"}))
        self.assertFalse(transport.compare_values({"seo_title": "Title"}, {"seo_title": "Title "}))
        self.assertTrue(transport.compare_values(expected, deepcopy(expected)))
        self.assertFalse(transport.compare_values({"long_description": "<script>bad()</script>"}, {"long_description": "bad()"}))

    def test_invalid_payload_never_opens_client_or_writes_stock(self):
        observed = remote(); good = transport.build_patch({"seo_title": "New"}, observed, ["seo_title"])["payload"]
        cases = []
        for key, value in (("stock", 0), ("variants", []), ("availability", "Unavailable"), ("product_id", 101)):
            item = deepcopy(good); item["products"][0][key] = value; cases.append(item)
        for key, value in (("title", "Other"), ("short_description", "Other"), ("long_description", "<img src='https://old.test/image'>")):
            item = deepcopy(good); item["products"][0]["descriptions"][0][key] = value; cases.append(item)
        item = deepcopy(good); item["products"][0]["descriptions"][0]["language"] = "en"; cases.append(item)
        item = deepcopy(good); item["products"][0]["descriptions"][0]["seo_title"] = ""; cases.append(item)
        item = deepcopy(good); item["products"][0]["code"] = "OTHER"; cases.append(item)
        for item in cases:
            with self.subTest(item=item), patch.object(transport.source, "_client") as client:
                with self.assertRaises(transport.source.SourceError):
                    transport._write("xtrek", observed["identity"], "0" * 64, item)
                client.assert_not_called()

    def test_one_write_and_native_parent_acknowledgement_are_required(self):
        observed = remote(); identity = observed["identity"]
        payload = transport.build_patch({"seo_title": "New"}, observed, ["seo_title"])["payload"]
        ack = {"products": [{"code": "FAMILY", "product_id": 101, "updated_yn": True}]}
        client = SimpleNamespace(session=SimpleNamespace(close=Mock()))
        with patch.object(transport.source, "_client", return_value=client), patch.object(transport.source, "_request", return_value=ack) as request:
            transport._write("xtrek", identity, "0" * 64, payload)
            request.assert_called_once_with(client, "put", "products", payload=payload)
            client.session.close.assert_called_once()
        ack["products"][0]["product_id"] = 999
        with patch.object(transport.source, "_client", return_value=client), patch.object(transport.source, "_request", return_value=ack) as request:
            with self.assertRaises(transport.source.SourceError) as exc:
                transport._write("xtrek", identity, "0" * 64, payload)
            self.assertTrue(exc.exception.uncertain)
            self.assertEqual(request.call_count, 1)

    def test_parent_ack_may_report_only_complete_unchanged_family_identities(self):
        identity = remote()["identity"]
        ack = {"products": [{"code": "FAMILY", "product_id": 101, "updated_yn": True,
            "variants": [{"code": "F-A", "variant_id": 201}, {"code": "F-B", "variant_id": 202}]}]}
        self.assertTrue(transport._acknowledged(ack, identity))
        ack["products"][0]["variants"][0]["variant_id"] = 999
        self.assertFalse(transport._acknowledged(ack, identity))
