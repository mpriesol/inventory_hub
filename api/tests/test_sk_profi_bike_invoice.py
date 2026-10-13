"""Synthetic invoice fixtures only; no customer PDF or financial records in Git."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from inventory_hub import config_io
from inventory_hub.adapters import sk_profi_bike_invoice as adapter
from inventory_hub.routers import suppliers, receiving_db


def invoice_text():
    # EUR 0.03 per line gives 0.01 tax each, but total invoice tax is 0.02.
    return """SK - PROFI BIKE, s.r.o.
Dátum vystavenia  Dátum dodania  Dátum splatnosti  Variabilný symbol
01.02.2026  01.02.2026  15.02.2026  99999999
Faktúra 9/26/00999
Interná referencia  Name  Množstvo  Jednotková cena  DPH  Celkom bez DPH  Celkom s DPH
SO-SYNTHETIC-ONLY
1 DEMO-A Test product alpha 1,00 Ks 0,03 23 % 0,03 € 0,04 €
2 DEMO-B Test product beta  1,00 Ks 0,03 23 % 0,03 € 0,04 €
3 DEMO-C Test product gamma 1,00 Ks 0,03 23 % 0,03 € 0,04 €
Suma bez DPH 0,09 €
DPH 23 % 0,02 €
Celkom 0,11 €
Splatná suma 0,11 €
"""


class SkpInvoiceParserTests(unittest.TestCase):
    def test_net_prices_blank_ean_and_invoice_level_tax_rounding(self):
        invoice = adapter.parse_invoice_text(invoice_text())
        self.assertEqual(invoice.number, "9/26/00999")
        self.assertEqual(invoice.key, "9_26_00999")
        self.assertEqual(invoice.issue_date, date(2026, 2, 1))
        self.assertEqual(invoice.due_date, date(2026, 2, 15))
        self.assertEqual(invoice.gross, Decimal("0.11"))
        self.assertEqual(sum(line.gross for line in invoice.lines), Decimal("0.12"))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "invoice.csv"
            path.write_bytes(invoice.csv_bytes())
            rows = receiving_db._parse_invoice_csv(path)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["supplier_sku"], "DEMO-A")
        self.assertEqual(rows[0]["ordered_qty"], Decimal("1"))
        self.assertEqual(rows[0]["unit_price"], Decimal("0.03"))
        self.assertEqual(rows[0]["ean"], "")

    def test_layout_reader_is_used_and_validated(self):
        page = SimpleNamespace(extract_text=Mock(return_value=invoice_text()))
        reader = SimpleNamespace(is_encrypted=False, pages=[page])
        with patch.object(adapter, "PdfReader", return_value=reader):
            result = adapter.parse_invoice_pdf(b"%PDF-1.7 synthetic fixture")
        page.extract_text.assert_called_once_with(extraction_mode="layout")
        self.assertEqual(len(result.lines), 3)

    def test_rejects_missing_rows_incorrect_prices_taxes_totals_and_currency(self):
        bad = [
            invoice_text().replace("SK - PROFI BIKE", "Different issuer"),
            invoice_text().replace("Faktúra 9/26/00999", "Faktúra ../unsafe"),
            invoice_text().replace("01.02.2026", "31.02.2026"),
            invoice_text().replace("2 DEMO-B", "4 DEMO-B"),
            invoice_text().replace("1,00 Ks 0,03", "2,00 Ks 0,03", 1),
            invoice_text().replace("0,03 € 0,04 €", "0,03 € 0,05 €", 1),
            invoice_text().replace("0,03 23 %", "0,03 20 %", 1),
            invoice_text().replace("DPH 23 % 0,02", "DPH 23 % 0,03"),
            invoice_text().replace("Suma bez DPH 0,09", "Suma bez DPH 0,10"),
            invoice_text().replace("Celkom 0,11", "Celkom 0,12"),
            invoice_text().replace("€", "USD"),
            invoice_text().replace("1,00 Ks", "unreadable Ks", 1),
            invoice_text().replace("1,00 Ks", "1,0001 Ks", 1),
            invoice_text().replace("DEMO-A", "A" * 97),
            invoice_text().replace("3 DEMO-C Test product gamma 1,00 Ks 0,03 23 % 0,03 € 0,04 €", ""),
        ]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(adapter.InvoiceParseError):
                adapter.parse_invoice_text(value)

    def test_pdf_errors_are_bounded_and_do_not_leak_reader_details(self):
        for content in (b"not a pdf", b"%PDF-" + b"x" * (20 * 1024 * 1024)):
            with self.assertRaises(adapter.InvoiceParseError):
                adapter.parse_invoice_pdf(content)
        with patch.object(adapter, "PdfReader", side_effect=ValueError("internal details")):
            with self.assertRaises(adapter.InvoiceParseError) as error:
                adapter.parse_invoice_pdf(b"%PDF-1.7 invalid")
        self.assertNotIn("internal details", str(error.exception))


class SkpInvoiceUploadTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(folder)
        self.enterContext(patch.object(suppliers, "DATA_ROOT", self.root))
        self.enterContext(patch.object(config_io, "DATA_ROOT", self.root))
        config = self.root / "suppliers/sk-profi-bike/config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"product_code_prefix":"SKP-"}')
        self.invoice = adapter.parse_invoice_text(invoice_text())
        self.reader = self.enterContext(patch.object(adapter, "parse_invoice_pdf", return_value=self.invoice))
        app = FastAPI()
        app.include_router(suppliers.router)
        self.client = self.enterContext(TestClient(app))
        self.invoice_root = config.parent / "invoices"
        self.index_path = self.invoice_root / "index.latest.json"
        self.invoice_id = "sk-profi-bike:9_26_00999"

    def upload(self, filename="arbitrary-download-name.pdf", **kwargs):
        return self.client.post("/suppliers/sk-profi-bike/upload-invoice", files={
            "file": (filename, b"%PDF-1.7 synthetic", "application/pdf"),
        }, **kwargs)

    def test_upload_uses_invoice_identity_date_and_receiving_csv(self):
        result = self.upload()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["filename"], "9_26_00999.pdf")
        self.assertFalse(result.json()["replayed"])
        entry = json.loads(self.index_path.read_text())[self.invoice_id]
        self.assertEqual(entry["issue_date"], "2026-02-01")
        self.assertEqual(entry["original_number"], "9/26/00999")
        self.assertFalse(entry["vat_included"])
        self.assertEqual((self.root / entry["csv_path"]).read_bytes(), self.invoice.csv_bytes())
        self.assertEqual(entry["status"], "new")

    def test_duplicate_filename_and_retry_preserve_completed_status_and_notes(self):
        self.assertEqual(self.upload().status_code, 200)
        index = json.loads(self.index_path.read_text())
        index[self.invoice_id].update(status="processed", note="Already received", current_session_id="123")
        self.index_path.write_text(json.dumps(index))
        before = self.index_path.read_bytes()
        result = self.upload("renamed.pdf")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["replayed"])
        self.assertEqual(self.index_path.read_bytes(), before)
        self.assertEqual(len(list((self.invoice_root / "csv").glob("*.csv"))), 1)

    def test_changed_same_number_rejects_without_overwriting(self):
        self.assertEqual(self.upload().status_code, 200)
        before = self.index_path.read_bytes()
        self.reader.return_value = adapter.parse_invoice_text(invoice_text().replace("Test product alpha", "Changed product"))
        response = self.upload()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.index_path.read_bytes(), before)

    def test_invalid_pdf_or_override_does_not_create_receiving_files(self):
        result = self.upload(params={"invoice_number": "OTHER"})
        self.assertEqual(result.status_code, 422, result.text)
        self.reader.side_effect = adapter.InvoiceParseError("Invalid document")
        result = self.upload()
        self.assertEqual(result.status_code, 422, result.text)
        self.assertFalse(self.invoice_root.exists())

    def test_recover_interrupted_upload_before_index(self):
        csv_path = self.invoice_root / "csv" / f"{self.invoice.key}.csv"
        csv_path.parent.mkdir(parents=True)
        csv_path.write_bytes(self.invoice.csv_bytes())
        response = self.upload()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["replayed"])
        self.assertEqual(len(json.loads(self.index_path.read_text())), 1)

    def test_interrupted_upload_cannot_pair_old_pdf_with_changed_rows(self):
        raw = self.invoice_root / "pdf" / f"{self.invoice.key}.pdf"
        raw.parent.mkdir(parents=True)
        raw.write_bytes(b"%PDF-1.7 different prior document")
        prior = adapter.parse_invoice_text(invoice_text().replace("DEMO-A", "DEMO-DIFFERENT"))
        self.reader.side_effect = [self.invoice, prior]
        response = self.upload()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertFalse((self.invoice_root / "csv").exists())
        self.assertFalse(self.index_path.exists())

    def test_concurrent_uploads_share_one_invoice(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: adapter.save_receiving_invoice(
                b"%PDF-1.7 synthetic", self.root, "sk-profi-bike"
            ), range(2)))
        self.assertEqual(sorted(row["replayed"] for row in responses), [False, True])
        self.assertEqual(list(json.loads(self.index_path.read_text())), [self.invoice_id])


if __name__ == "__main__":
    unittest.main()
