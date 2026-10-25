"""Synthetic source lineage, exact money and upload-boundary regressions."""
import hashlib
import json
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient

from inventory_hub.adapters import sk_profi_bike_invoice as skp, zookee_invoice, paul_lange_web as pl
from inventory_hub.adapters.northfinder_xlsx_parser import parse_xlsx_to_csv
from inventory_hub.db_models import ReceivingStatus
from inventory_hub.routers import receiving_db as receiving, suppliers
from inventory_hub.services import receiving_source as source
from test_zookee import pdf, isdoc


class ReceivingSourceTests(TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode() if isinstance(content, str) else content)
        return path

    def parse(self, content):
        return receiving._parse_invoice_csv(self.write("input.csv", content))

    def test_bad_numeric_values_and_malformed_rows_are_never_zero(self):
        for value in ("", "abc", "NaN", "Infinity", "-1", "1e2", "1.0001", "1000000000"):
            with self.subTest(quantity=value), self.assertRaises(source.ReceivingSourceError):
                self.parse(f"SCM;QTY;PRICE\nA;{value};1\n")
        for value in ("abc", "NaN", "Infinity", "-1", "1e2", "0.00001", "100000000"):
            with self.subTest(price=value), self.assertRaises(source.ReceivingSourceError):
                self.parse(f"SCM;QTY;PRICE\nA;1;{value}\n")
        for content in ('"SCM;QTY\n', 'SCM;QTY;QTY\nA;1;2\n', 'SCM;QTY\nA;1;2\n'):
            with self.subTest(content=content), self.assertRaises(source.ReceivingSourceError):
                self.parse(content)
        self.assertEqual(self.parse("SCM;QTY;PRICE\nA;0;0\n")[0]["unit_price"], D(0))
        self.assertIsNone(self.parse("SCM;QTY;PRICE\nA;1;\n")[0]["unit_price"])

    def test_currency_and_gross_annotations_cannot_be_ignored(self):
        for column, value in (("CURRENCY", "CZK"), ("VAT_INCLUDED", "true")):
            with self.subTest(column=column), self.assertRaises(source.ReceivingSourceError):
                self.parse(f"SCM;QTY;PRICE;{column}\nA;1;100;{value}\n")

    def test_zookee_original_proves_eur_net_and_csv_changes_are_blocked(self):
        saved = skp.save_receiving_invoice(pdf(isdoc()), self.root, "zookee", parser=zookee_invoice.parse_invoice_pdf)
        path = self.root / saved["csv_path"]
        verified = source.load_source(self.root, "zookee", "9990001", path, receiving._parse_invoice_csv)
        self.assertEqual(verified.rows[0]["unit_price"], D("10"))
        self.assertEqual(verified.snapshot()["currency"], "EUR")
        self.assertFalse(verified.snapshot()["vat_included"])
        path.write_text(path.read_text().replace("10.0000", "100.0000"))
        with self.assertRaises(source.ReceivingSourceError) as raised:
            source.load_source(self.root, "zookee", "9990001", path, receiving._parse_invoice_csv)
        self.assertEqual(raised.exception.code, "receiving_source_changed")

    def test_paul_lange_requires_real_download_proof_and_explicit_mapping(self):
        content = "Article;Label;Count;NetEUR\nA;Synthetic;2;0.00\n"
        path = self.write("suppliers/paul-lange/invoices/csv/1.csv", content)
        config = {"adapter_settings": {"currency": "EUR", "mapping": {
            "invoice_to_canon": {"SCM": "Article", "TITLE": "Label", "QTY": "Count", "UNIT_PRICE_EX": "NetEUR"},
            "postprocess": {"unit_price_source": "ex"}}}}
        self.write("suppliers/paul-lange/config.json", json.dumps(config))
        entry = {"number": "1", "csv_path": "invoices/csv/1.csv", "sha1": hashlib.sha1(content.encode()).hexdigest(),
                 "issue_date_source": "fallback:download", "layout_used": "flat"}
        index = self.write("suppliers/paul-lange/invoices/index.latest.json", json.dumps({"1": entry}))
        with self.assertRaises(source.ReceivingSourceError):
            source.load_source(self.root, "paul-lange", "1", path, receiving._parse_invoice_csv)
        entry["receiving_source"] = "paul_lange_download_v1"
        index.write_text(json.dumps({"1": entry}))
        with self.assertRaises(source.ReceivingSourceError):
            source.load_source(self.root, "paul-lange", "1", path, receiving._parse_invoice_csv)
        entry["receiving_document_number"] = "1"
        index.write_text(json.dumps({"1": entry}))
        first = source.load_source(self.root, "paul-lange", "1", path, receiving._parse_invoice_csv)
        self.assertEqual(first.rows[0]["unit_price"], D(0))
        second_path = self.write("suppliers/paul-lange/invoices/csv/2.csv", content)
        second = {**entry, "number": "2", "receiving_document_number": "2", "csv_path": "invoices/csv/2.csv"}
        index.write_text(json.dumps({"1": entry, "2": second}))
        other = source.load_source(self.root, "paul-lange", "2", second_path, receiving._parse_invoice_csv)
        self.assertEqual(first.csv_hash, other.csv_hash)
        self.assertNotEqual(first.source_hash, other.source_hash)
        # Changing an index number cannot claim a different supplier document.
        second["number"] = "3"
        index.write_text(json.dumps({"1": entry, "2": second}))
        with self.assertRaises(source.ReceivingSourceError):
            source.load_source(self.root, "paul-lange", "3", second_path, receiving._parse_invoice_csv)
        del config["adapter_settings"]["currency"]
        self.write("suppliers/paul-lange/config.json", json.dumps(config))
        with self.assertRaises(source.ReceivingSourceError) as raised:
            source.load_source(self.root, "paul-lange", "1", path, receiving._parse_invoice_csv)
        self.assertEqual(raised.exception.code, "receiving_source_currency_unsupported")

    def northfinder(self, price=0):
        path = self.root / "source.xlsx"
        pd.DataFrame([{"Kód": "001", "Názov": "00123", "Množstvo": 2,
            "Jednotková cena v EUR": price, "Spolu v EUR": 0, "Spolu v EUR s DPH": 0, "EAN": "0012345678905"}]).to_excel(path, index=False)
        return path

    def test_northfinder_keeps_zero_bonus_and_leading_zero_but_rejects_invalid_cost(self):
        target = self.root / "normalized.csv"
        self.assertTrue(parse_xlsx_to_csv(self.northfinder(), target)["success"])
        row = receiving._parse_invoice_csv(target)[0]
        self.assertEqual((row["unit_price"], row["ean"]), (D(0), "0012345678905"))
        self.assertEqual((row["supplier_sku"], row["description"]), ("001", "00123"))
        target.unlink()
        result = parse_xlsx_to_csv(self.northfinder("invalid"), target)
        self.assertFalse(result["success"])
        self.assertFalse(target.exists())

    def test_direct_csv_and_foreign_supplier_spreadsheet_stop_before_file_write(self):
        from inventory_hub import config_io
        self.write("suppliers/synthetic/config.json", "{}")
        with patch.object(suppliers, "DATA_ROOT", self.root), patch.object(config_io, "DATA_ROOT", self.root):
            app = FastAPI(); app.include_router(suppliers.router)
            with TestClient(app) as client:
                for extension, code in (("csv", "receiving_direct_csv_unsupported"),
                                        ("xlsx", "receiving_spreadsheet_supplier_unsupported")):
                    response = client.post("/suppliers/synthetic/upload-invoice",
                        files={"file": (f"document.{extension}", b"synthetic", "application/octet-stream")})
                    self.assertEqual(response.status_code, 422)
                    self.assertEqual(response.json()["detail"]["code"], code)
            self.assertFalse((self.root / "suppliers/synthetic/invoices").exists())

    def test_paul_lange_legacy_refresh_verifies_bytes_without_resetting_receiving(self):
        raw = b"SCM;QTY;PRICE\nA;1;1\n"
        path = self.write("suppliers/paul-lange/invoices/csv/1.csv", raw)
        entry = {"number": "1", "status": "processed", "current_session_id": 7, "csv_path": "invoices/csv/1.csv"}
        index = self.write("suppliers/paul-lange/invoices/index.latest.json", json.dumps({"paul-lange:1": entry}))
        response = SimpleNamespace(text="invoice list", url="https://supplier.invalid/list", raise_for_status=lambda: None)
        session = Mock(); session.get.return_value = response
        def download(_session, _login, _url, target, **kwargs):
            target.write_bytes(raw)
            return True
        with patch.object(pl, "build_session", return_value=session), patch.object(pl, "_looks_like_login_page", return_value=False), \
             patch.object(pl, "enumerate_invoice_pages", return_value=[response.url]), \
             patch.object(pl, "list_invoice_links", return_value=[("https://supplier.invalid/1.csv", "1.csv")]), \
             patch.object(pl, "try_download_csv", side_effect=download) as fetched:
            result = pl.refresh_invoices_web(self.root, "paul-lange", pl.LoginConfig())
            self.assertEqual(result.failed, 0); fetched.assert_called_once()
            proven = json.loads(index.read_text())["paul-lange:1"]
            self.assertEqual((proven["status"], proven["current_session_id"]), ("processed", 7))
            self.assertEqual(proven["receiving_source"], "paul_lange_download_v1")
            self.assertEqual(proven["receiving_document_number"], "1")
            self.assertEqual(path.read_bytes(), raw)
            fetched.reset_mock()
            pl.refresh_invoices_web(self.root, "paul-lange", pl.LoginConfig())
            fetched.assert_not_called()

            # A fresh response that differs must not overwrite source or status,
            # nor promote the old file to verified download provenance.
            index.write_text(json.dumps({"paul-lange:1": entry}))
            def changed(_session, _login, _url, target, **kwargs):
                target.write_bytes(raw.replace(b"A;1;1", b"A;1;2"))
                return True
            fetched.side_effect = changed
            result = pl.refresh_invoices_web(self.root, "paul-lange", pl.LoginConfig())
            self.assertEqual(result.failed, 1)
            self.assertEqual(json.loads(index.read_text())["paul-lange:1"], entry)
            self.assertEqual(path.read_bytes(), raw)


class ReceivingSourceSnapshotTests(IsolatedAsyncioTestCase):
    async def test_distinct_verified_documents_can_have_identical_csv_rows(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            verified = []
            for number in ("9990001", "9990002"):
                raw = pdf(isdoc().replace(b"9990001", number.encode()))
                saved = skp.save_receiving_invoice(raw, root, "zookee", parser=zookee_invoice.parse_invoice_pdf)
                verified.append(source.load_source(root, "zookee", number, root / saved["csv_path"],
                                                   receiving._parse_invoice_csv))
            first, second = verified
            self.assertEqual(first.csv_hash, second.csv_hash)
            self.assertNotEqual(first.source_hash, second.source_hash)
            existing = SimpleNamespace(id=7, source_hash=first.source_hash,
                session_data={"receiving_source": first.snapshot()}, invoice_number=first.document_number,
                status=ReceivingStatus.new)
            db = AsyncMock(); db.scalars.return_value = [existing]
            await source.ensure_unique(db, 1, second, root)
            with self.assertRaises(source.ReceivingSourceError) as raised:
                await source.ensure_unique(db, 1, first, root)
            self.assertEqual(raised.exception.code, "receiving_source_duplicate")

    async def test_snapshot_detects_changed_database_price_and_legacy_without_source(self):
        row = {"supplier_sku": "A", "ean": "", "description": "Test", "ordered_qty": D(1), "unit_price": D(10)}
        proof = source.ReceivingSource([row], "a" * 64, "b" * 64, "1", "synthetic")
        session = SimpleNamespace(source_hash=proof.source_hash, invoice_currency="EUR", invoice_file_path=None,
            session_data={"receiving_source": proof.snapshot()}, lines=[SimpleNamespace(line_number=1, **row)])
        await source.validate_session(AsyncMock(), session, "test", Path("/unused"), None)
        session.lines[0].unit_price = D(11)
        with self.assertRaises(source.ReceivingSourceError) as raised:
            await source.validate_session(AsyncMock(), session, "test", Path("/unused"), None)
        self.assertEqual(raised.exception.code, "receiving_source_changed")
        session.session_data = {}
        with self.assertRaises(source.ReceivingSourceError) as raised:
            await source.validate_session(AsyncMock(), session, "test", Path("/unused"), None)
        self.assertEqual(raised.exception.code, "receiving_source_unverified")

    async def test_duplicate_has_existing_receipt_details_and_missing_unrelated_file_is_not_match(self):
        proof = source.ReceivingSource([], "a" * 64, "b" * 64, "1", "synthetic")
        existing = SimpleNamespace(id=7, source_hash="a" * 64, session_data={}, invoice_number="OLD",
                                   status=ReceivingStatus.completed)
        db = AsyncMock(); db.scalars.return_value = [existing]
        with self.assertRaises(source.ReceivingSourceError) as raised:
            await source.ensure_unique(db, 1, proof, Path("/unused"))
        self.assertEqual(raised.exception.detail, {"code": "receiving_source_duplicate", "session_id": 7,
                                                  "invoice_number": "OLD", "status": "completed"})
        existing.source_hash, existing.invoice_file_path = None, "/missing/document.csv"
        await source.ensure_unique(db, 1, proof, Path("/unused"))
