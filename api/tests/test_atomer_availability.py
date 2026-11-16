"""Exact Atomer source/CSV boundaries without a network or stock writer."""

import csv
import io
import unittest
from xml.etree.ElementTree import Element, SubElement, tostring
from pydantic import ValidationError
from inventory_hub.atomer_availability_types import Override, ExportInput
from inventory_hub.services import atomer_availability as service
from inventory_hub.services import atomer_availability_source as source


def buyers(*rows):
    root = Element("PRODUCTS")
    for values in rows:
        node = SubElement(root, "PRODUCT")
        defaults = {
            "PRODUCT_ID": "RECEIPT-A",
            "PARENT_ID": "",
            "PRODUCT_NAME": "Test product",
            "EAN": "",
            "VISIBILITY": "1",
            "AVAILABILITY": "overíme",
            "MAIN_IMAGE": "https://example.invalid/test.jpg",
        }
        for key, value in {**defaults, **values}.items():
            SubElement(node, key).text = value
    return tostring(root, encoding="utf-8")


def projection(pid=1, quantity="2", **extra):
    return dict(
        product_id=pid,
        sku="RECEIPT-A",
        quantity=quantity,
        offer={},
        reason=None,
        **extra
    )


class AtomerAvailabilitySourceTests(unittest.TestCase):
    def test_catalog_codes_and_parent_codes_remain_exact_in_delta_csv(self):
        _, catalog = source.parse_buyers(
            buyers({"PRODUCT_ID": "0000123", "PARENT_ID": "0000-FAMILY"})
        )
        rows = source.assemble(catalog, {"0000123": projection()})
        output = list(
            csv.reader(
                io.StringIO(service.csv_bytes(rows).decode("utf-8-sig")), delimiter=";"
            )
        )
        self.assertEqual(output[1], ["0000123", "0000-FAMILY", "1", "SKLADOM"])
        self.assertEqual(
            len(output), 2
        )  # Missing parent is context, never an invented update.
        self.assertEqual(
            next(r for r in rows if r["kind"] == "parent")["current_visibility"], None
        )

    def test_unknown_stock_does_not_erase_existing_skladom_or_reveal_hidden(self):
        _, catalog = source.parse_buyers(
            buyers({"AVAILABILITY": "SKLADOM", "VISIBILITY": "0"})
        )
        row = source.assemble(catalog, {"RECEIPT-A": projection(quantity=None)})[0]
        self.assertEqual(
            (row["status"], row["proposed_visibility"], row["proposed_availability"]),
            ("blocked", False, "SKLADOM"),
        )
        self.assertIn("stock_unconfirmed", row["reasons"])
        self.assertEqual(
            len(
                list(
                    csv.reader(
                        io.StringIO(service.csv_bytes([row]).decode("utf-8-sig")),
                        delimiter=";",
                    )
                )
            ),
            1,
        )

    def test_known_zero_uses_fresh_supplier_but_never_hides_or_reveals_item(self):
        _, catalog = source.parse_buyers(buyers({"VISIBILITY": "0"}))
        state = projection(quantity="0")
        state["offer"] = {"fresh": True, "available": True, "label": "2 až 4 dni"}
        row = source.assemble(catalog, {"RECEIPT-A": state})[0]
        self.assertEqual(
            (row["proposed_visibility"], row["proposed_availability"]),
            (False, "2 až 4 dni"),
        )
        state["offer"]["fresh"] = False
        row = source.assemble(catalog, {"RECEIPT-A": state})[0]
        self.assertEqual(
            (row["proposed_visibility"], row["proposed_availability"]),
            (False, "overíme"),
        )

    def test_hidden_stocked_variant_reveals_verified_parent(self):
        _, catalog = source.parse_buyers(
            buyers(
                {"PRODUCT_ID": "FAMILY", "VISIBILITY": "0"},
                {"PRODUCT_ID": "CHILD", "PARENT_ID": "FAMILY", "VISIBILITY": "0"},
            )
        )
        rows = source.assemble(catalog, {"CHILD": projection()})
        self.assertEqual(
            {r["code"]: r["proposed_visibility"] for r in rows},
            {"FAMILY": True, "CHILD": True},
        )
        self.assertEqual({r["status"] for r in rows}, {"changed"})
        self.assertEqual(
            len(
                list(
                    csv.reader(
                        io.StringIO(service.csv_bytes(rows).decode("utf-8-sig")),
                        delimiter=";",
                    )
                )
            ),
            3,
        )

    def test_hidden_variant_does_not_claim_parent_visibility_without_evidence(self):
        _, catalog = source.parse_buyers(
            buyers({"PRODUCT_ID": "CHILD", "PARENT_ID": "MISSING", "VISIBILITY": "0"})
        )
        rows = source.assemble(catalog, {"CHILD": projection()})
        child = next(r for r in rows if r["code"] == "CHILD")
        self.assertEqual(child["status"], "blocked")
        self.assertIn("parent_state_unknown", child["reasons"])
        with self.assertRaises(source.ExportError):
            service.edited_rows(rows, [Override(code="CHILD", visibility=True)])

    def test_duplicate_source_identity_cannot_be_manually_exported(self):
        _, catalog = source.parse_buyers(
            buyers(
                {"PRODUCT_ID": "FAMILY", "VISIBILITY": "0"},
                {"PRODUCT_ID": "FAMILY", "VISIBILITY": "1"},
                {"PRODUCT_ID": "CHILD", "PARENT_ID": "FAMILY", "VISIBILITY": "0"},
            )
        )
        rows = source.assemble(catalog, {"CHILD": projection()})
        with self.assertRaises(source.ExportError):
            service.edited_rows(rows, [Override(code="FAMILY", visibility=True)])

    def test_self_parent_remains_one_blocked_row_and_cannot_be_exported(self):
        _, catalog = source.parse_buyers(
            buyers({"PRODUCT_ID": "SELF", "PARENT_ID": "SELF"})
        )
        self.assertIn("source_parent_invalid", catalog["SELF"]["reasons"])
        rows = source.assemble(catalog, {"SELF": projection()})
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            (rows[0]["code"], rows[0]["parent_code"], rows[0]["status"]),
            ("SELF", "SELF", "blocked"),
        )
        self.assertIn("source_parent_invalid", rows[0]["reasons"])
        self.assertEqual(
            len(
                list(
                    csv.reader(
                        io.StringIO(service.csv_bytes(rows).decode("utf-8-sig")),
                        delimiter=";",
                    )
                )
            ),
            1,
        )
        with self.assertRaises(source.ExportError):
            service.edited_rows(rows, [Override(code="SELF", visibility=True)])

    def test_manual_overrides_are_local_and_preserve_source_state(self):
        _, catalog = source.parse_buyers(buyers({"VISIBILITY": "0"}))
        rows = source.assemble(catalog, {"RECEIPT-A": projection()})
        edited = service.edited_rows(
            rows,
            [
                Override(
                    code="RECEIPT-A", visibility=False, availability="Na predobjednanie"
                )
            ],
        )
        self.assertEqual(rows[0]["proposed_availability"], "SKLADOM")
        self.assertTrue(rows[0]["proposed_visibility"])
        self.assertFalse(edited[0]["proposed_visibility"])
        self.assertEqual(edited[0]["current_availability"], "overíme")
        self.assertEqual(edited[0]["proposed_availability"], "Na predobjednanie")
        self.assertTrue(edited[0]["manual_override"])

    def test_source_invalid_xml_dtd_and_duplicate_fields_are_rejected(self):
        for raw in (
            b'<!DOCTYPE PRODUCTS [<!ENTITY e SYSTEM "file:///private">]><PRODUCTS/>',
            b"<SHOP><SHOPITEM/></SHOP>",
            b"<PRODUCTS><PRODUCT><PRODUCT_ID>A</PRODUCT_ID><PRODUCT_ID>B</PRODUCT_ID></PRODUCT></PRODUCTS>",
            b"<PRODUCTS>",
        ):
            with self.subTest(raw=raw[:30]), self.assertRaises(ValueError):
                source.parse_buyers(raw)

    def test_invalid_source_codes_are_preserved_and_isolated_from_valid_rows(self):
        _, catalog = source.parse_buyers(
            buyers(
                {"PRODUCT_ID": " PADDED "},
                {"PRODUCT_ID": "CONTROL\tCODE"},
                {"PRODUCT_ID": "VALID"},
            )
        )
        self.assertIn(" PADDED ", catalog)
        self.assertNotIn("PADDED", catalog)
        self.assertIn("source_identity_invalid", catalog[" PADDED "]["reasons"])
        self.assertIn("source_identity_invalid", catalog["CONTROL\tCODE"]["reasons"])
        rows = source.assemble(catalog, {code: projection() for code in catalog})
        output = list(
            csv.reader(
                io.StringIO(service.csv_bytes(rows).decode("utf-8-sig")), delimiter=";"
            )
        )
        self.assertEqual([r[0] for r in output[1:]], ["VALID"])

    def test_input_rejects_unknown_fields_controls_and_duplicate_override_codes(self):
        for kwargs in (
            {"code": "A", "availability": "\nSKLADOM"},
            {"code": "A", "availability": " overíme"},
            {"code": "A", "availability": ""},
            {"code": "A", "visibility": 1},
            {"code": "A", "stock": 7},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                Override(**kwargs)
        with self.assertRaises(ValidationError):
            ExportInput(expected_revision=1, overrides=[{"code": "A"}, {"code": "A"}])

    def test_admin_export_supplies_hidden_items_but_rejects_missing_parent_when_complete(
        self,
    ):
        raw = (
            "Kat. číslo;Kat. číslo rodiča;Interné číslo;Viditeľný;Názov tovaru;EAN kód;Dodacia doba;Obrázky\n"
            "FAMILY;;1;0;Family;;overíme;\n"
            "CHILD;FAMILY;2;0;Child;;do 5 dní;\n"
        ).encode()
        catalog = source.apply_admin({}, raw, True)
        self.assertEqual(set(catalog), {"FAMILY", "CHILD"})
        self.assertFalse(catalog["CHILD"]["current_visibility"])
        self.assertEqual(catalog["CHILD"]["current_availability"], "do 5 dní")
        incomplete = raw.replace(
            b"FAMILY;;1;0;Family;;over", b"OTHER;;1;0;Family;;over"
        )
        with self.assertRaises(source.ExportError):
            source.apply_admin({}, incomplete, True)

    def test_native_parent_reference_groups_by_catalog_but_csv_preserves_import_reference(
        self,
    ):
        raw = (
            "Kat. číslo;Kat. číslo rodiča;Interné číslo;Viditeľný;Názov tovaru;EAN kód;Dodacia doba;Obrázky\n"
            "FAMILY;;1;0;Family;;overíme;\n"
            "CHILD;1;2;0;Child;;do 5 dní;\n"
        ).encode()
        catalog = source.apply_admin({}, raw, True)
        self.assertEqual(catalog["CHILD"]["parent_code"], "FAMILY")
        self.assertEqual(catalog["CHILD"]["export_parent_code"], "1")
        rows = source.assemble(catalog, {"CHILD": projection()})
        child = next(row for row in rows if row["code"] == "CHILD")
        self.assertEqual(child["parent_name"], "Family")
        output = list(
            csv.reader(
                io.StringIO(service.csv_bytes(rows).decode("utf-8-sig")), delimiter=";"
            )
        )
        self.assertIn(["CHILD", "1", "1", "SKLADOM"], output)
        self.assertIn(["FAMILY", "", "1", "overíme"], output)

    def test_complete_admin_rejects_quarantined_identity_instead_of_claiming_complete_coverage(
        self,
    ):
        raw = (
            "Kat. číslo;Kat. číslo rodiča;Interné číslo;Viditeľný;Názov tovaru;EAN kód;Dodacia doba;Obrázky\n"
            "VALID;;1;1;Valid;;overíme;\n"
            "BAD\x01CODE;;2;1;Invalid;;overíme;\n"
        ).encode()
        self.assertEqual(source.parse_admin(raw).quarantined, ["2"])
        with self.assertRaisesRegex(source.ExportError, "admin_incomplete"):
            source.apply_admin({}, raw, True)

    def test_blank_catalog_parents_never_become_exportable_internal_ids(self):
        raw = (
            "Kat. číslo;Kat. číslo rodiča;Interné číslo;Viditeľný;Názov tovaru;EAN kód;Dodacia doba;Obrázky\n"
            ";;1;0;No catalog A;;overíme;\n"
            ";;2;0;No catalog B;;overíme;\n"
            "CHILD;1;3;0;Child;;do 5 dní;\n"
        ).encode()
        catalog = source.apply_admin({}, raw, True)
        for code in ("1", "2"):
            self.assertIn("source_identity_invalid", catalog[code]["reasons"])
        rows = source.assemble(catalog, {"CHILD": projection()})
        output = list(
            csv.reader(
                io.StringIO(service.csv_bytes(rows).decode("utf-8-sig")), delimiter=";"
            )
        )
        self.assertEqual(len(output), 1)
        for code in ("1", "2"):
            with self.assertRaises(source.ExportError):
                service.edited_rows(rows, [Override(code=code, visibility=True)])
