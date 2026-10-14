"""Legacy JSON receipt files obey the same bounded file access as PostgreSQL."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import HTTPException

from inventory_hub import config_io
from inventory_hub.routers import receiving


class LegacyReceivingPathTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory())
        self.root = Path(folder) / "data"
        self.outside = Path(folder) / "private.json"
        self.outside.write_text('{"private": "unchanged"}')
        self.enterContext(patch.object(receiving.settings, "INVENTORY_DATA_ROOT", self.root))
        self.enterContext(patch.object(config_io, "DATA_ROOT", self.root))
        config_io.save_supplier("synthetic", {"product_code_prefix": "TEST-"})
        self.invoice_dir = self.root / "suppliers/synthetic/invoices/csv"
        self.invoice_dir.mkdir(parents=True)
        (self.invoice_dir / "Faktúra 2026.01.csv").write_text("SCM;TITLE;QTY\nITEM;Item;2\n")

    def create(self):
        return receiving.create_session("synthetic", "synthetic:Faktúra 2026.01")

    def test_valid_receipt_scan_pause_resume_and_completion_stay_in_supplier_directory(self):
        created = self.create()
        identifier = created["session_id"]
        self.assertEqual(created["invoice_no"], "Faktúra 2026.01")
        receiving.set_line_quantity("synthetic", identifier, 0, 2, None)
        receiving.pause_session("synthetic", identifier)
        self.assertTrue(receiving.get_active_session("synthetic", created["invoice_no"])["session"]["is_paused"])
        receiving.resume_session("synthetic", identifier)
        result = receiving.finalize_session("synthetic", identifier, True)
        self.assertEqual(result["total_received"], 2)
        history = receiving.get_receiving_history("synthetic", created["invoice_no"])
        self.assertEqual(len(history["sessions"]), 1)
        self.assertEqual(len(history["completed"]), 1)
        self.assertEqual(self.outside.read_text(), '{"private": "unchanged"}')

    def test_invoice_paths_encoded_aliases_globs_and_wrong_supplier_never_reach_parser(self):
        values = ("../private", "/tmp/private", r"..\private", "*", "[ab]", "?", ".hidden",
                  "%2e%2e%2fprivate", "%252fprivate", "other:invoice", "synthetic:../private")
        with patch.object(receiving, "_parse_invoice_rows") as parse:
            for value in values:
                with self.subTest(value=value), self.assertRaises(HTTPException) as error:
                    receiving.create_session("synthetic", value)
                self.assertEqual(error.exception.status_code, 400)
            parse.assert_not_called()

    def test_every_session_lookup_rejects_paths_and_globs(self):
        operations = (
            lambda value: receiving.scan_code("synthetic", value, "ITEM", 1),
            lambda value: receiving.get_summary("synthetic", value),
            lambda value: receiving.set_line_quantity("synthetic", value, 0, 1, None),
            lambda value: receiving.accept_all_items("synthetic", value, True),
            lambda value: receiving.reset_all_items("synthetic", value),
            lambda value: receiving.finalize_session("synthetic", value, True),
            lambda value: receiving.pause_session("synthetic", value),
            lambda value: receiving.resume_session("synthetic", value),
        )
        for operation in operations:
            for value in ("../private", "*", "%2fprivate", r"..\private"):
                with self.subTest(operation=operation, value=value), self.assertRaises(HTTPException) as error:
                    operation(value)
                self.assertEqual(error.exception.status_code, 400)

    def test_symlink_invoice_and_session_cannot_be_read_or_mutated(self):
        (self.invoice_dir / "linked.csv").symlink_to(self.outside)
        with self.assertRaises(HTTPException):
            receiving.create_session("synthetic", "linked")
        created = self.create()
        path = receiving._session_path("synthetic", created["invoice_no"], created["session_id"])
        path.unlink()
        path.symlink_to(self.outside)
        with self.assertRaises(HTTPException):
            receiving.get_summary("synthetic", created["session_id"])
        with self.assertRaises(HTTPException):
            receiving.reset_all_items("synthetic", created["session_id"])
        self.assertEqual(receiving.get_receiving_history("synthetic", created["invoice_no"])["sessions"], [])
        self.assertFalse(receiving.get_active_session("synthetic", created["invoice_no"])["has_session"])
        self.assertEqual(self.outside.read_text(), '{"private": "unchanged"}')

    def test_symlink_receiving_directory_cannot_redirect_session_writes(self):
        directory = receiving._supplier_path("synthetic", "receiving")
        directory.symlink_to(self.outside.parent, target_is_directory=True)
        with self.assertRaises(HTTPException):
            self.create()
        with self.assertRaises(HTTPException):
            receiving.get_summary("synthetic", "123")
        self.assertFalse((self.outside.parent / "Faktúra 2026.01").exists())

    def test_symlink_completion_summary_and_index_temporary_file_are_not_overwritten(self):
        created = self.create()
        identifier, number = created["session_id"], created["invoice_no"]
        directory = receiving._receiving_dir("synthetic", number)
        (directory / f"completed_{identifier}.json").symlink_to(self.outside)
        with self.assertRaises(HTTPException):
            receiving.finalize_session("synthetic", identifier, True)
        (directory / "summary_latest.json").symlink_to(self.outside)
        with self.assertRaises(HTTPException):
            receiving.get_summary("synthetic", identifier)
        index = self.invoice_dir.parent / "index.latest.json"
        original = json.dumps({f"synthetic:{number}": {"number": number, "status": "new"}})
        index.write_text(original)
        index.with_suffix(".tmp").symlink_to(self.outside)
        with patch("builtins.print"):
            self.assertFalse(receiving._update_invoice_status("synthetic", number, "processed"))
        self.assertEqual(index.read_text(), original)
        self.assertEqual(self.outside.read_text(), '{"private": "unchanged"}')
