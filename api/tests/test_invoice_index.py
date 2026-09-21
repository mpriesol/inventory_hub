"""Invoice identity regressions; all files live in a temporary directory."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from inventory_hub import config_io
from inventory_hub.routers import invoices, receiving, suppliers
from inventory_hub.settings import settings


class InvoiceIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for target in (config_io, suppliers):
            self.enterContext(patch.object(target, "DATA_ROOT", self.root))
        self.enterContext(patch.object(settings, "INVENTORY_DATA_ROOT", self.root))
        self.supplier = "test-supplier"
        self.number = "TEST-INVOICE"
        self.invoice_id = f"{self.supplier}:{self.number}"
        self.index_path = self.root / "suppliers" / self.supplier / "invoices" / "index.latest.json"
        self.index_path.parent.mkdir(parents=True)
        (self.index_path.parent.parent / "config.json").write_text("{}", encoding="utf-8")
        self.app = FastAPI()
        self.app.include_router(suppliers.router)
        self.app.include_router(invoices.router)
        self.client = self.enterContext(TestClient(self.app))
        self.note_url = f"/suppliers/{self.supplier}/invoices/{self.number}/note"

    def read_index(self):
        return json.loads(self.index_path.read_text(encoding="utf-8"))

    def upload_invoice(self):
        response = self.client.post(
            f"/suppliers/{self.supplier}/upload-invoice",
            files={"file": (f"{self.number}.csv", b"SCM;TITLE;QTY\nTEST-SKU;Test;1\n", "text/csv")},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def seed_legacy_invoice(self):
        entry = {
            "supplier": self.supplier,
            "invoice_id": self.number,
            "number": self.number,
            "csv_path": f"invoices/csv/{self.number}.csv",
            "issue_date": "2026-01-01",
            "status": "in_progress",
            "current_session_id": "test-session",
            "note": "Existing note",
        }
        self.index_path.write_text(json.dumps({self.number: entry}), encoding="utf-8")
        return entry

    def prepare_invoice(self):
        # CSV generation is unrelated to identity; no supplier or shop calls.
        with patch.object(invoices, "prepare_from_invoice", return_value={"outputs": {}}):
            response = self.client.post("/runs/prepare", json={
                "supplier_ref": self.supplier,
                "shop_ref": "test-shop",
                "invoice_relpath": f"invoices/csv/{self.number}.csv",
            })
        self.assertEqual(response.status_code, 200, response.text)

    def test_upload_note_and_completion_keep_one_invoice(self):
        self.upload_invoice()
        self.assertEqual(list(self.read_index()), [self.invoice_id])
        uploaded = self.read_index()[self.invoice_id]
        response = self.client.post(self.note_url, json={"note": "Receiving test"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(receiving._update_invoice_status(
            self.supplier, self.number, "in_progress", {"current_session_id": "test-session"}
        ))
        self.prepare_invoice()
        index = self.read_index()
        self.assertEqual(list(index), [self.invoice_id])
        entry = index[self.invoice_id]
        self.assertEqual(entry["csv_path"], uploaded["csv_path"])
        self.assertEqual(entry["issue_date"], uploaded["issue_date"])
        self.assertEqual(entry["note"], "Receiving test")
        self.assertEqual(entry["status"], "processed")
        listed = self.client.get(f"/suppliers/{self.supplier}/invoices/index").json()
        self.assertEqual(listed["count"], 1)

    def test_legacy_note_is_read_without_changing_its_identity(self):
        self.seed_legacy_invoice()
        response = self.client.get(self.note_url)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["note"], "Existing note")
        self.assertEqual(response.json()["status"], "in_progress")
        self.assertEqual(list(self.read_index()), [self.number])

    def test_legacy_note_update_preserves_receiving_state_and_csv(self):
        original = self.seed_legacy_invoice()
        response = self.client.post(self.note_url, json={"note": "Updated note"})
        self.assertEqual(response.status_code, 200, response.text)
        index = self.read_index()
        self.assertEqual(list(index), [self.number])
        self.assertEqual(index[self.number], {**original, "note": "Updated note"})

    def test_legacy_completion_does_not_create_a_second_invoice(self):
        original = self.seed_legacy_invoice()
        self.prepare_invoice()
        index = self.read_index()
        self.assertEqual(list(index), [self.number])
        self.assertEqual(index[self.number]["status"], "processed")
        self.assertEqual(index[self.number]["csv_path"], original["csv_path"])
        self.assertEqual(index[self.number]["note"], original["note"])

    def test_same_invoice_number_in_another_supplier_is_untouched(self):
        self.seed_legacy_invoice()
        other_path = self.root / "suppliers" / "other-test-supplier" / "invoices" / "index.latest.json"
        other_path.parent.mkdir(parents=True)
        other_path.write_text(json.dumps({self.number: {"note": "Other supplier"}}), encoding="utf-8")
        before = other_path.read_bytes()
        response = self.client.post(self.note_url, json={"note": "Updated note"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(other_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
