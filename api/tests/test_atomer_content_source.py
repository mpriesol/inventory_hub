"""Source/HTML boundaries for an existing-product content pilot, without writes."""
import hashlib
import io
import unittest
from unittest.mock import patch
from uuid import uuid4
from xml.sax.saxutils import escape

from pydantic import ValidationError

from inventory_hub.adapters import atomer_content as source
from inventory_hub.atomer_content_types import AtomerContentBatch, AtomerContentItem


def row(code="leaf", parent="", ean="0012345678905", description="<p><strong>Text</strong></p>", **extra):
    values = {"PRODUCT_ID": code, "PARENT_ID": parent, "EAN": ean, "MANUFACTURER": "Brand",
              "DESCRIPTION_LONG": description, "SEO_TITLE": "Title", "SEO_DESCIPTION": "Meta", **extra}
    seo = {key: values.pop(key) for key in ("SEO_TITLE", "SEO_DESCIPTION")}
    return ("<PRODUCT>" + "".join(f"<{key}>{escape(value)}</{key}>" for key, value in values.items())
            + "<SEO>" + "".join(f"<{key}>{escape(value)}</{key}>" for key, value in seo.items()) + "</SEO></PRODUCT>")


def xml(*rows):
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?><PRODUCTS>" + "".join(rows) + "</PRODUCTS>").encode()


class SourceTests(unittest.TestCase):
    def parse(self, data, selected=None):
        return source.parse_selected(io.BytesIO(data), selected or ["leaf"])

    def error(self, expected, data, selected=None):
        with self.assertRaises(source.AtomerSourceError) as caught:
            self.parse(data, selected)
        self.assertEqual("atomer_source_" + expected, caught.exception.code)

    def test_whole_upload_hash_and_complete_selected_family_keep_source_values(self):
        data = xml(row("a", "family"), row("other"), row("b", "family"))
        result = self.parse(data, ["family"])
        self.assertEqual(result["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(result["size_bytes"], len(data))
        self.assertEqual(result["total_rows"], 3)
        selected = result["groups"]["family"]["rows"]
        self.assertEqual([r["code"] for r in selected], ["a", "b"])
        self.assertEqual([r["source_position"] for r in selected], [1, 3])
        self.assertEqual(selected[0]["ean"], "0012345678905")
        self.assertIn("<EAN>0012345678905</EAN>", selected[0]["raw_xml"])
        self.assertEqual(source.source_values(result["groups"]["family"], ["seo_description"]), {"seo_description": "Meta"})

    def test_duplicates_inside_or_outside_selected_family_are_not_overwritten(self):
        self.error("duplicate_code", xml(row(), row()))
        self.error("duplicate_code", xml(row("a", "family"), row("a", "elsewhere")), ["family"])

    def test_family_identifier_also_used_as_standalone_is_rejected(self):
        self.error("mixed_family", xml(row("a", "family"), row("family")), ["family"])

    def test_missing_selection_is_explicit(self):
        self.error("selection_missing", xml(row("other")))

    def test_unrelated_invalid_code_does_not_block_verified_selection(self):
        result = self.parse(xml(row(), row(" other ")))
        self.assertEqual(result["total_rows"], 2)
        self.assertEqual(len(result["groups"]["leaf"]["rows"]), 1)

    def test_truncated_trailing_source_invalidates_previously_seen_selection(self):
        self.error("invalid_xml", xml(row())[:-1])
        self.error("invalid_xml", xml(row()) + b"<broken>")

    def test_wrong_root_or_nested_products_rejected(self):
        self.error("invalid_root", b"<SHOPITEMS/>" )
        self.error("invalid_structure", b"<PRODUCTS><wrapper>" + row().encode() + b"</wrapper></PRODUCTS>")

    def test_ambiguous_identity_fields_not_first_value_wins(self):
        data = xml(row()).replace(b"</PRODUCT_ID>", b"</PRODUCT_ID><PRODUCT_ID>other</PRODUCT_ID>")
        self.error("ambiguous_field", data)

    def test_doctype_and_entities_are_rejected_before_parsing(self):
        payload = b'<!DOCTYPE PRODUCTS [<!ENTITY secret SYSTEM "file:///etc/passwd">]><PRODUCTS><PRODUCT><PRODUCT_ID>&secret;</PRODUCT_ID></PRODUCT></PRODUCTS>'
        self.error("dtd_forbidden", payload)
        self.error("dtd_forbidden", b"<!ENTITY foo 'bar'>" + xml(row()))

    def test_utf16_and_non_utf8_declarations_cannot_bypass_dtd_guard(self):
        self.error("unsupported_encoding", xml(row()).decode().replace("UTF-8", "UTF-16").encode("utf-16"))
        self.error("unsupported_encoding", xml(row()).replace(b"UTF-8", b"ISO-8859-1"))
        self.error("unsupported_encoding", xml(row()).replace(b"Brand", b"Br\xffnd"))

    def test_utf8_bom_keeps_byte_hash(self):
        data = b"\xef\xbb\xbf" + xml(row())
        self.assertEqual(self.parse(data)["sha256"], hashlib.sha256(data).hexdigest())

    def test_bounded_upload_and_family(self):
        with patch.object(source, "MAX_BYTES", 10):
            self.error("too_large", xml(row()))
        with patch.object(source, "MAX_FAMILY_ROWS", 1):
            self.error("family_too_large", xml(row("a", "family"), row("b", "family")), ["family"])

    def test_no_stock_fields_can_be_selected(self):
        group = self.parse(xml(row(COUNT_ON_STOCK="1")))["groups"]["leaf"]
        with self.assertRaises(source.AtomerSourceError):
            source.source_values(group, ["COUNT_ON_STOCK"])
        self.assertNotIn("COUNT_ON_STOCK", group["rows"][0])

    def test_different_shared_parent_values_block_instead_of_pick_first(self):
        group = self.parse(xml(row("a", "family"), row("b", "family", SEO_TITLE="Other")), ["family"])["groups"]["family"]
        with self.assertRaisesRegex(source.AtomerSourceError, "family_content_conflict"):
            source.source_values(group, ["seo_title"])
        self.assertEqual(source.source_values(group, ["seo_description"]), {"seo_description": "Meta"})

    def test_empty_or_markup_only_source_never_erases_target(self):
        for description in ("", "   ", "<p> </p>"):
            group = self.parse(xml(row(description=description)))["groups"]["leaf"]
            with self.assertRaisesRegex(source.AtomerSourceError, "invalid_content"):
                source.source_values(group, ["long_description"])


class HtmlTests(unittest.TestCase):
    def test_safe_original_html_retained_without_sanitizer(self):
        content = '<p style="text-align: justify;"><span style="color: #993300;"><strong>Opis</strong></span></p><table class="table table-bordered"><tbody><tr><td>A</td><td>B</td></tr></tbody></table>'
        self.assertEqual(source.source_values({"rows": [{"long_description": content}]}, ["long_description"])["long_description"], content)

    def test_fingerprint_ignores_attribute_order_entities_and_void_serialization(self):
        a = '<p class="text" title="x">A &amp; B<br /></p>'
        b = '<p title="x" class="text">A &#38; B<br></p>'
        self.assertEqual(source.html_fingerprint(a), source.html_fingerprint(b))

    def test_same_text_does_not_hide_removed_formatting_or_table_cells(self):
        self.assertNotEqual(source.html_fingerprint("<p><strong>A</strong></p>"), source.html_fingerprint("<p>A</p>"))
        self.assertNotEqual(source.html_fingerprint("<table><tr><td>A</td><td>B</td></tr></table>"), source.html_fingerprint("<table><tr><td>AB</td></tr></table>"))

    def test_unsafe_elements_attributes_and_styles_block_without_cleaning(self):
        values = ['<script>alert(1)</script>', '<p onclick="x()">A</p>', '<img src="https://example.com/a.jpg">',
                  '<a href="javascript:alert(1)">A</a>', '<p style="background-image:url(https://example.com/a)">A</p>',
                  '<p style="color:expression(alert(1))">A</p>', '<p style="color:r\\65 d">A</p>',
                  '<div data-message-id="x">A</div>', '<svg><title>A</title></svg>']
        for value in values:
            with self.subTest(value=value), self.assertRaises(source.AtomerSourceError):
                source.html_fingerprint(value)

    def test_malformed_html_rejected(self):
        with self.assertRaises(source.AtomerSourceError):
            source.html_fingerprint("<p><strong>A</p></strong>")


class SelectionTests(unittest.TestCase):
    def item(self, **changes):
        return {"request_id": str(uuid4()), "hub_sku": "0001", "source_code": "0001",
                "fields": ["long_description"], **changes}

    def test_strict_identity_and_field_allowlist(self):
        value = AtomerContentItem.model_validate(self.item())
        self.assertEqual(value.hub_sku, "0001")
        for changes in ({"hub_sku": 1}, {"source_code": " 0001"}, {"fields": ["stock"]},
                        {"fields": ["seo_title", "seo_title"]}, {"stock": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                AtomerContentItem.model_validate(self.item(**changes))

    def test_duplicate_requests_or_source_selections_rejected(self):
        a = self.item()
        for b in (self.item(request_id=a["request_id"], source_code="different"), self.item()):
            with self.assertRaises(ValidationError):
                AtomerContentBatch.model_validate({"items": [a, b]})


if __name__ == "__main__":
    unittest.main()
