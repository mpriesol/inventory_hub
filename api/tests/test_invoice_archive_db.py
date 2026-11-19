"""External invoice identity, source evidence and indexing with real PostgreSQL."""
import asyncio
from contextlib import asynccontextmanager
import csv
from decimal import Decimal as D
import hashlib
from io import BytesIO, StringIO
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from zipfile import ZipFile
from uuid import uuid4

import pandas as pd
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from fastapi import FastAPI, UploadFile
from httpx import ASGITransport, AsyncClient

from invoice_db_fixture import DATABASE_REQUIRED, TEST_URL, InvoiceDatabaseCase
from inventory_hub.access import operator_access
from inventory_hub.adapters import northfinder_invoice as nf
from inventory_hub.adapters import sk_profi_bike_invoice as skp
from inventory_hub.database import get_session
from inventory_hub.invoice_archive_types import InvoiceReconcileInput
from inventory_hub.invoice_processing_types import InvoiceProcessingApply, InvoiceProcessingPreview
from inventory_hub.routers import invoices_unified as routes
from inventory_hub.routers import receiving_db, suppliers
from inventory_hub.services import invoice_archive as archive
from inventory_hub.services import invoice_export
from inventory_hub.services import invoice_processing, invoice_upload, receiving_source
from test_sk_profi_bike_invoice import invoice_text
from test_northfinder_invoice import invoice_pages


@unittest.skipUnless(TEST_URL, DATABASE_REQUIRED)
class InvoiceArchiveDatabaseTests(InvoiceDatabaseCase):
    def source(self, **changes):
        return {
            "direction": "purchase", "source_kind": "supplier", "source_key": "document-001",
            "supplier_id": self.supplier_id,
            "metadata": {"invoice_number": "SUP/2026/001", "invoice_date": "2026-02-01",
                         "due_date": "2026-02-15", "currency": "EUR", "total_without_vat": "0.09",
                         "vat_amount": "0.02", "total_with_vat": "0.11"},
            "lines": [{"line_number": 1, "source_line_key": "leaf-001", "ean": "5901234123457",
                       "barcodes": ["5901234123457"], "supplier_sku": "RAW-001",
                       "product_name": "Synthetic invoice line", "quantity": "3.000", "unit": "ks",
                       "unit_price": "0.0300", "total_price": "0.09", "vat_rate": "23.00",
                       "unit_price_with_vat": "0.0400", "total_price_with_vat": "0.12",
                       "matched_product_id": self.product_id}],
            "files": [{"filename": "invoice.pdf", "content": b"%PDF-1.4\nSynthetic invoice fixture\n%%EOF",
                       "role": "original", "file_type": "pdf"}],
            "source_data": {"fixture": "source document", "source_id": "001"},
            **changes,
        }

    async def ingest(self, source):
        async with self.sessions() as db:
            result = await archive.ingest_invoice(db, **source)
            await db.commit()
            return result

    async def test_ingest_preserves_source_rounding_lines_and_original_without_stock(self):
        result = await self.ingest(self.source())
        row = (await self.rows("SELECT * FROM uploaded_invoices WHERE id=:id", {"id": result["id"]}))[0]
        self.assertEqual((row["direction"], row["source_kind"], row["supplier_id"]),
                         ("purchase", "supplier", self.supplier_id))
        self.assertEqual((row["invoice_number"], row["currency"]), ("SUP/2026/001", "EUR"))
        self.assertEqual((row["total_without_vat"], row["vat_amount"], row["total_with_vat"]),
                         (D("0.09"), D("0.02"), D("0.11")))
        line = (await self.rows("SELECT * FROM uploaded_invoice_lines WHERE invoice_id=:id",
                                {"id": result["id"]}))[0]
        # Preserve the issuer's document rounding, even when its line gross differs.
        self.assertEqual(line["total_price_with_vat"], D("0.12"))
        self.assertEqual((line["quantity"], line["unit_price"], line["matched_product_id"]),
                         (D("3.000"), D("0.0300"), self.product_id))
        self.assertIn("5901234123457", line["barcode_values"])
        files = await self.rows("SELECT * FROM invoice_files WHERE invoice_id=:id", {"id": result["id"]})
        self.assertEqual(len(files), 1)
        original = files[0]
        content = self.source()["files"][0]["content"]
        self.assertEqual((original["role"], original["sha256"], original["size_bytes"]),
                         ("original", hashlib.sha256(content).hexdigest(), len(content)))
        path = Path(original["file_path"])
        self.assertTrue(path.resolve().is_relative_to(self.data_root.resolve()))
        self.assertEqual(path.read_bytes(), content)
        self.assertIsNone(row["receiving_session_id"])
        await self.assert_no_inventory_effects()

    async def test_concurrent_replay_has_one_document_one_line_and_one_original(self):
        first, second = await asyncio.gather(self.ingest(self.source()), self.ingest(self.source()))
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(sorted([first["replayed"], second["replayed"]]), [False, True])
        for table in ("uploaded_invoices", "uploaded_invoice_lines", "invoice_files"):
            self.assertEqual(await self.scalar(f"SELECT count(*) FROM {table}"), 1)
        await self.assert_no_inventory_effects()

    async def test_changed_source_facts_conflict_without_replacing_evidence(self):
        original = self.source()
        result = await self.ingest(original)
        for changed in (
            self.source(metadata={**original["metadata"], "total_with_vat": "0.12"}),
            self.source(lines=[{**original["lines"][0], "quantity": "4.000"}]),
            self.source(files=[{**original["files"][0], "content": b"different source original"}]),
        ):
            with self.subTest(changed=changed.keys()), self.assertRaises(archive.InvoiceArchiveError):
                await self.ingest(changed)
        self.assertEqual(await self.scalar("SELECT total_with_vat FROM uploaded_invoices WHERE id=:id",
                                           {"id": result["id"]}), D("0.11"))
        self.assertEqual(await self.scalar("SELECT quantity FROM uploaded_invoice_lines WHERE invoice_id=:id",
                                           {"id": result["id"]}), D("3.000"))
        self.assertEqual(await self.scalar("SELECT count(*) FROM invoice_files"), 1)

    async def test_source_payment_observations_can_refresh_without_duplicate_document(self):
        original = self.source()
        first = await self.ingest(original)
        source_hash = await self.scalar("SELECT source_hash FROM uploaded_invoices WHERE id=:id", {"id": first["id"]})
        updated = self.source(metadata={**original["metadata"], "source_payment_status": "paid",
                                        "source_paid_amount": "0.11", "source_paid_at": "2026-02-14T09:00:00Z"})
        replay = await self.ingest(updated)
        self.assertEqual((replay["id"], replay["replayed"]), (first["id"], True))
        row = (await self.rows("SELECT source_hash,source_paid_amount,source_payment_status,payment_revision "
                               "FROM uploaded_invoices WHERE id=:id", {"id": first["id"]}))[0]
        self.assertEqual((row["source_hash"], row["source_paid_amount"], row["source_payment_status"]),
                         (source_hash, D("0.11"), "paid"))
        self.assertEqual(row["payment_revision"], 1)
        self.assertEqual(await self.scalar("SELECT count(*) FROM invoice_payments"), 0)

    async def test_regenerated_original_keeps_first_evidence_and_adds_observed_source_version(self):
        source = self.source()
        first = await self.ingest(source)
        primary = await self.scalar("SELECT file_path FROM uploaded_invoices WHERE id=:id", {"id": first["id"]})
        old_content = Path(primary).read_bytes()
        refreshed = self.source(
            files=[{**source["files"][0], "content": b"%PDF-1.4\nSame issued invoice with PAID stamp\n%%EOF"}],
            metadata={**source["metadata"], "source_payment_status": "paid", "source_paid_amount": "0.11"},
        )
        result = await self.ingest(refreshed)
        self.assertEqual((result["id"], result["replayed"], result["files_count"], result["lines_count"]),
                         (first["id"], True, 2, 1))
        self.assertEqual(await self.scalar("SELECT file_path FROM uploaded_invoices WHERE id=:id", {"id": first["id"]}), primary)
        self.assertEqual(Path(primary).read_bytes(), old_content)
        self.assertEqual(await self.scalar("SELECT source_paid_amount FROM uploaded_invoices WHERE id=:id",
                                           {"id": first["id"]}), D("0.11"))
        self.assertEqual(await self.scalar("SELECT count(*) FROM invoice_payments"), 0)

    async def test_accountant_bundle_keeps_original_versions_and_current_source_settlement(self):
        source = self.source()
        first = await self.ingest(source)
        refreshed = self.source(
            files=[{**source['files'][0], 'content': b'%PDF-1.4\nUpdated payment stamp\n%%EOF'}],
            metadata={**source['metadata'], 'source_payment_status': 'partial', 'source_paid_amount': '0.05'},
        )
        await self.ingest(refreshed)
        async with self.sessions() as db:
            bundle = await invoice_export.export(db, [first['id'], first['id']])
        try:
            with ZipFile(bundle) as downloaded:
                document_names = [name for name in downloaded.namelist() if name != 'manifest.csv']
                self.assertEqual(len(document_names), 2)
                self.assertEqual({downloaded.read(name) for name in document_names},
                                 {source['files'][0]['content'], refreshed['files'][0]['content']})
                manifest = list(csv.DictReader(StringIO(downloaded.read('manifest.csv').decode('utf-8-sig')), delimiter=';'))
                self.assertEqual(len(manifest), 2)
                for row in manifest:
                    self.assertEqual((row['currency'], row['total_with_vat'], row['payment_status'],
                                      row['paid_amount'], row['outstanding_amount']),
                                     ('EUR', '0.11', 'partial', '0.05', '0.06'))
                    self.assertIn(row['file'], document_names)
        finally:
            bundle.close()
        self.assertEqual(await self.scalar('SELECT count(*) FROM invoice_payments'), 0)
        await self.assert_no_inventory_effects()

    async def test_replay_preserves_original_product_link_when_current_mapping_changes(self):
        source = self.source(direction="sale", source_kind="shop", supplier_id=None, shop_id=self.shop_id)
        async with self.sessions() as db:
            await db.execute(text("""INSERT INTO shop_products
                (product_id,shop_id,external_code,external_product_id,is_variant)
                VALUES (:product,:shop,'SHOP-001','1001',false)"""),
                {"product": self.product_id, "shop": self.shop_id})
            await db.commit()
        first = await self.ingest(source)
        async with self.sessions() as db:
            other_product = await db.scalar(text("INSERT INTO products(sku,name) VALUES ('ARCHIVE-OTHER','Other card') RETURNING id"))
            await db.execute(text("UPDATE shop_products SET product_id=:product WHERE shop_id=:shop"),
                             {"product": other_product, "shop": self.shop_id})
            await db.commit()
        source["lines"][0]["matched_product_id"] = other_product
        replay = await self.ingest(source)
        self.assertEqual((replay["id"], replay["replayed"]), (first["id"], True))
        self.assertEqual(await self.scalar("SELECT matched_product_id FROM uploaded_invoice_lines WHERE invoice_id=:id",
                                           {"id": first["id"]}), self.product_id)
        self.assertEqual(await self.scalar("SELECT count(*) FROM uploaded_invoice_lines"), 1)
        await self.assert_no_inventory_effects()

    async def test_source_keys_are_scoped_to_supplier_or_shop(self):
        first = await self.ingest(self.source())
        second = await self.ingest(self.source(supplier_id=self.other_supplier_id))
        third = await self.ingest(self.source(direction="sale", source_kind="shop", supplier_id=None,
                                              shop_id=self.shop_id))
        self.assertEqual(len({first["id"], second["id"], third["id"]}), 3)
        self.assertEqual(await self.scalar("SELECT count(*) FROM uploaded_invoices WHERE source_key='document-001'"), 3)
        sale = (await self.rows("SELECT direction,supplier_id,shop_id FROM uploaded_invoices WHERE id=:id",
                               {"id": third["id"]}))[0]
        self.assertEqual(dict(sale), {"direction": "sale", "supplier_id": None, "shop_id": self.shop_id})
        await self.assert_no_inventory_effects()

    async def test_unknown_money_and_credit_note_remain_source_facts(self):
        unknown = await self.ingest(self.source(source_key="unknown", metadata={"invoice_number": "UNKNOWN"}))
        row = (await self.rows("SELECT currency,total_amount,total_with_vat,source_paid_amount FROM uploaded_invoices "
                               "WHERE id=:id", {"id": unknown["id"]}))[0]
        self.assertEqual(dict(row), {"currency": None, "total_amount": None, "total_with_vat": None,
                                     "source_paid_amount": None})
        credit = await self.ingest(self.source(source_key="credit", lines=[{
            "line_number": 1, "quantity": "-1", "unit_price": "100", "total_price": "-100",
            "unit": "ks", "product_name": "Synthetic external credit",
        }], metadata={"invoice_number": "CREDIT-001", "currency": "EUR", "document_type": "credit_note",
                      "total_without_vat": "-100", "vat_amount": "-23", "total_with_vat": "-123"}))
        self.assertEqual(await self.scalar("SELECT total_with_vat FROM uploaded_invoices WHERE id=:id",
                                           {"id": credit["id"]}), D("-123.00"))
        await self.assert_no_inventory_effects()

    async def test_database_rejects_ambiguous_archive_owner(self):
        result = await self.ingest(self.source())
        async with self.sessions() as db:
            with self.assertRaises(IntegrityError):
                await db.execute(text("UPDATE uploaded_invoices SET shop_id=:shop WHERE id=:id"),
                                 {"shop": self.shop_id, "id": result["id"]})
        self.assertIsNone(await self.scalar("SELECT shop_id FROM uploaded_invoices WHERE id=:id", {"id": result["id"]}))

    async def test_upload_route_reuses_identical_original_and_keeps_unparsed_totals_unknown(self):
        content = b"%PDF-1.4\nSynthetic unparsed upload\n%%EOF"

        async def upload(filename):
            async with self.sessions() as db:
                return await routes.upload_invoice(
                    supplier_code="archive-fixture", file=UploadFile(filename=filename, file=BytesIO(content)),
                    invoice_number="UPLOAD-001", invoice_date=None, due_date=None, vat_included=True, db=db,
                )

        first, replay = await upload("original.pdf"), await upload("renamed-copy.pdf")
        self.assertEqual((first["id"], replay["id"], replay["replayed"]), (first["id"], first["id"], True))
        row = (await self.rows("SELECT source_kind,original_filename,currency,total_amount,total_with_vat,is_parsed "
                               "FROM uploaded_invoices"))[0]
        self.assertEqual(dict(row), {"source_kind": "upload", "original_filename": "original.pdf",
                                     "currency": None, "total_amount": None, "total_with_vat": None, "is_parsed": False})
        self.assertEqual(await self.scalar("SELECT count(*) FROM invoice_files"), 1)
        await self.assert_no_inventory_effects()

    async def test_unparsed_upload_can_be_archived_before_its_invoice_number_is_known(self):
        async with self.sessions() as db:
            result = await routes.upload_invoice(
                supplier_code="archive-fixture", file=UploadFile(filename="receipt.pdf",
                    file=BytesIO(b"%PDF-1.4\nSynthetic unnumbered original\n%%EOF")),
                invoice_number=None, invoice_date=None, due_date=None, vat_included=True, db=db,
            )
        row = (await self.rows("SELECT invoice_number,invoice_date,currency,total_with_vat FROM uploaded_invoices "
                               "WHERE id=:id", {"id": result["id"]}))[0]
        self.assertEqual(dict(row), {"invoice_number": None, "invoice_date": None, "currency": None,
                                     "total_with_vat": None})
        await self.assert_no_inventory_effects()

    def index(self, entries):
        folder = self.data_root / "suppliers" / "archive-fixture" / "invoices"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "index.latest.json").write_text(json.dumps(entries), encoding="utf-8")
        return folder

    async def reconcile(self, **changes):
        async with self.sessions() as db:
            result = await archive.reconcile_supplier(db, InvoiceReconcileInput(
                supplier_code="archive-fixture", **changes))
            await db.commit()
            return result

    async def test_reconcile_pages_indexed_lines_and_replays_without_creating_receipts(self):
        folder = self.index({key: {"number": key, "csv_path": f"invoices/csv/{key}.csv",
                                  "currency": "EUR", "issue_date": "2026-02-01",
                                  "issue_date_source": "document"} for key in ("INV-A", "INV-B")})
        (folder / "csv").mkdir()
        for key in ("INV-A", "INV-B"):
            (folder / "csv" / f"{key}.csv").write_text(
                f"SCM;TITLE;QTY;PRICE;EAN\n{key};Synthetic line;2;12,3400;5901234123457\n", encoding="utf-8")
        first = await self.reconcile(limit=1)
        self.assertEqual((first["scanned"], first["created"], first["conflicts"], first["next_after_key"]),
                         (1, 1, [], "INV-A"))
        second = await self.reconcile(limit=1, after_key=first["next_after_key"])
        self.assertEqual((second["created"], second["conflicts"], second["next_after_key"]), (1, [], None))
        replay = await self.reconcile()
        self.assertEqual((replay["created"], replay["existing"], replay["conflicts"]), (0, 2, []))
        rows = await self.rows("SELECT quantity,unit_price,barcode_values FROM uploaded_invoice_lines ORDER BY id")
        self.assertEqual([(row["quantity"], row["unit_price"]) for row in rows], [(D("2"), D("12.3400"))] * 2)
        self.assertTrue(all("5901234123457" in row["barcode_values"] for row in rows))
        self.assertEqual(await self.scalar("SELECT count(*) FROM uploaded_invoices WHERE total_with_vat IS NULL"), 2)
        await self.assert_no_inventory_effects()

    async def test_reconcile_native_comma_csv_with_blank_preamble_and_supplier_mapping(self):
        folder = self.index([
            {"number": "NATIVE-CSV", "status": "new", "csv_path": "invoices/csv/native.csv"},
            {"number": "NATIVE-CSV", "status": "processed", "csv_path": "invoices/csv/native.csv"},
        ])
        (folder.parent / "config.json").write_text(json.dumps({"adapter_settings": {"mapping": {
            "invoice_to_canon": {"SCM": "SČM", "EAN": "EAN", "TITLE": "Názov",
                                 "QTY": "Počet kusov", "UNIT_PRICE_EX": "Jednotková cena"}}}}))
        (folder / "csv").mkdir()
        original = (' \r\nSČM,EAN,Názov,Počet kusov,Jednotková cena\r\n'
                    '00001,5901234123457,"Synthetic, part","2,5","1,2500"\r\n').encode('utf-8-sig')
        (folder / "csv/native.csv").write_bytes(original)
        report = await self.reconcile()
        self.assertEqual((report["scanned"], report["created"], report["conflicts"]), (1, 1, []))
        row = (await self.rows("SELECT supplier_sku,ean,product_name,quantity,unit_price,vat_rate,unit "
                               "FROM uploaded_invoice_lines"))[0]
        self.assertEqual((row["supplier_sku"], row["ean"], row["product_name"], row["quantity"], row["unit_price"]),
                         ("00001", "5901234123457", "Synthetic, part", D("2.5"), D("1.2500")))
        self.assertIsNone(row["vat_rate"])
        self.assertIsNone(row["unit"])
        header = (await self.rows("SELECT currency,total_amount,total_with_vat,vat_amount FROM uploaded_invoices"))[0]
        self.assertTrue(all(value is None for value in header.values()))
        original_path = await self.scalar("SELECT file_path FROM invoice_files WHERE role='original'")
        self.assertEqual(Path(original_path).read_bytes(), original)
        replay = await self.reconcile()
        self.assertEqual((replay["existing"], replay["conflicts"]), (1, []))
        self.assertEqual(await self.scalar("SELECT count(*) FROM uploaded_invoice_lines"), 1)
        await self.assert_no_inventory_effects()

    async def test_reconcile_reports_missing_or_cross_supplier_files_and_keeps_valid_document(self):
        folder = self.index({
            "GOOD": {"number": "GOOD", "csv_path": "invoices/csv/good.csv"},
            "MISSING": {"number": "MISSING", "csv_path": "invoices/csv/missing.csv"},
            "OTHER": {"number": "OTHER", "csv_path": "suppliers/archive-other/invoices/csv/private.csv"},
        })
        (folder / "csv").mkdir()
        (folder / "csv/good.csv").write_text("SCM;QTY\nONLY-GOOD;1\n", encoding="utf-8")
        other = self.data_root / "suppliers/archive-other/invoices/csv/private.csv"
        other.parent.mkdir(parents=True)
        other.write_text("SCM;QTY\nOTHER-SUPPLIER;1\n", encoding="utf-8")
        result = await self.reconcile()
        self.assertEqual((result["scanned"], result["created"]), (3, 1))
        self.assertEqual({item["source_key"] for item in result["conflicts"]}, {"MISSING", "OTHER"})
        self.assertEqual(await self.scalar("SELECT supplier_sku FROM uploaded_invoice_lines"), "ONLY-GOOD")
        await self.assert_no_inventory_effects()

    async def test_reconcile_status_only_index_duplicates_archive_once_with_all_evidence(self):
        entries = [
            {"number": "INV", "status": "new", "pdf_path": "invoices/raw/original.pdf",
             "csv_path": "invoices/csv/original.csv", "total_with_vat": "12.30", "currency": "EUR"},
            {"number": "INV", "status": "processed", "note": "Retain source note",
             "pdf_path": "invoices/raw/copy.pdf", "csv_path": "invoices/csv/copy.csv",
             "total_with_vat": "12.30", "currency": "EUR"},
        ]
        folder = self.index(entries)
        (folder / "raw").mkdir()
        (folder / "csv").mkdir()
        original = b"%PDF-1.4\nSynthetic shared invoice\n%%EOF"
        for name in ("original", "copy"):
            (folder / "raw" / f"{name}.pdf").write_bytes(original)
            ending = "\n" if name == "original" else "\r\n"
            (folder / "csv" / f"{name}.csv").write_bytes(f"SCM;QTY;PRICE{ending}ITEM;2;5.00{ending}".encode())
        report = await self.reconcile()
        self.assertEqual((report["scanned"], report["created"], report["conflicts"]), (1, 1, []))
        row = (await self.rows("SELECT source_data,total_with_vat,receiving_session_id FROM uploaded_invoices"))[0]
        data = json.loads(row["source_data"]) if isinstance(row["source_data"], str) else row["source_data"]
        self.assertEqual([candidate["entry"] for candidate in data["index_candidates"]], entries)
        self.assertEqual(row["total_with_vat"], D("12.30"))
        self.assertIsNone(row["receiving_session_id"])
        self.assertEqual(await self.scalar("SELECT count(*) FROM uploaded_invoice_lines"), 1)
        self.assertEqual(await self.scalar("SELECT count(*) FROM invoice_files"), 3)
        original_path = await self.scalar("SELECT file_path FROM invoice_files WHERE role='original'")
        self.assertEqual(Path(original_path).read_bytes(), original)
        replay = await self.reconcile()
        self.assertEqual((replay["scanned"], replay["existing"], replay["conflicts"]), (1, 1, []))
        await self.assert_no_inventory_effects()

    async def test_reconcile_conflicts_are_per_document_and_pages_count_unique_source_keys(self):
        def entry(key, **changes):
            return {"number": key, "csv_path": "invoices/csv/shared.csv", "total_with_vat": "12.30", **changes}

        folder = self.index([
            entry("A", status="new"), entry("A", total_with_vat="12.31"),
            entry("B", status="new"), entry("B", status="processed"),
            entry("C", raw_path="invoices/raw/first.pdf"), entry("C", raw_path="invoices/raw/changed.pdf"),
            entry("D"),
        ])
        (folder / "csv").mkdir()
        (folder / "csv/shared.csv").write_text("SCM;QTY;PRICE\nITEM;1;10.00\n")
        (folder / "raw").mkdir()
        for name in ("first", "changed"):
            (folder / "raw" / f"{name}.pdf").write_bytes(f"%PDF-1.4\nSynthetic {name}\n%%EOF".encode())
        first = await self.reconcile(limit=2)
        self.assertEqual((first["scanned"], first["created"], first["next_after_key"]), (2, 1, "B"))
        self.assertEqual(first["conflicts"], [{"source_key": "A", "code": "invoice_index_ambiguous"}])
        second = await self.reconcile(limit=2, after_key=first["next_after_key"])
        self.assertEqual((second["scanned"], second["created"], second["next_after_key"]), (2, 1, None))
        self.assertEqual(second["conflicts"], [{"source_key": "C", "code": "invoice_index_ambiguous"}])
        self.assertEqual([row["source_key"] for row in await self.rows(
            "SELECT source_key FROM uploaded_invoices ORDER BY source_key")], ["B", "D"])
        self.assertEqual(await self.scalar("SELECT count(*) FROM uploaded_invoice_lines"), 2)
        await self.assert_no_inventory_effects()

    async def test_upload_archive_filters_unrelated_ambiguous_keys_before_reading_candidates(self):
        folder = self.index([
            {"number": "BAD", "total_with_vat": "1.00", "csv_path": "invoices/csv/missing.csv"},
            {"number": "BAD", "total_with_vat": "2.00", "csv_path": "invoices/csv/missing.csv"},
            {"number": "GOOD", "csv_path": "invoices/csv/good.csv"},
        ])
        (folder / "csv").mkdir()
        (folder / "csv/good.csv").write_text("SCM;QTY\nGOOD-ITEM;1\n")

        @asynccontextmanager
        async def session_context():
            async with self.sessions() as db:
                yield db
                await db.commit()

        with patch("inventory_hub.database.get_session_context", session_context), patch.object(
                archive.settings, "USE_POSTGRES", True):
            result = await archive.archive_after_upload("archive-fixture", {
                "invoice_number": "GOOD", "csv_path": "invoices/csv/good.csv"})
        self.assertEqual((result["status"], result["scanned"], result["created"], result["conflicts"]),
                         ("completed", 1, 1, []))
        self.assertEqual(await self.scalar("SELECT source_key FROM uploaded_invoices"), "GOOD")
        await self.assert_no_inventory_effects()

    async def test_reconcile_receiving_alias_ambiguity_keeps_other_document_and_exact_receipt_link(self):
        folder = self.index([
            {"original_number": "AMBIGUOUS", "number": "RECEIPT-A", "csv_path": "invoices/csv/shared.csv"},
            {"original_number": "AMBIGUOUS", "number": "RECEIPT-B", "csv_path": "invoices/csv/shared.csv"},
            {"number": "GOOD", "csv_path": "invoices/csv/shared.csv"},
        ])
        (folder / "csv").mkdir()
        (folder / "csv/shared.csv").write_text("SCM;QTY\nITEM;1\n")
        async with self.sessions() as db:
            warehouse_id = await db.scalar(text(
                "INSERT INTO warehouses(code,name) VALUES ('archive-test','Archive test') RETURNING id"))
            receipts = {}
            for number in ("RECEIPT-A", "RECEIPT-B", "GOOD"):
                receipts[number] = await db.scalar(text("""INSERT INTO receiving_sessions
                    (supplier_id,warehouse_id,invoice_number) VALUES (:supplier,:warehouse,:number) RETURNING id"""),
                    {"supplier": self.supplier_id, "warehouse": warehouse_id, "number": number})
            await db.commit()
        report = await self.reconcile()
        self.assertEqual((report["scanned"], report["created"]), (2, 1))
        self.assertEqual(report["conflicts"], [{"source_key": "AMBIGUOUS", "code": "invoice_receiving_ambiguous"}])
        row = (await self.rows("SELECT source_key,receiving_session_id FROM uploaded_invoices"))[0]
        self.assertEqual((row["source_key"], row["receiving_session_id"]), ("GOOD", receipts["GOOD"]))
        self.assertEqual(await self.scalar("SELECT count(*) FROM receiving_sessions"), 3)
        self.assertEqual(await self.scalar("SELECT count(*) FROM stock_movements"), 0)

    async def test_indexed_upload_date_is_not_fabricated_as_invoice_issue_date(self):
        folder = self.index({"LEGACY": {"number": "LEGACY", "issue_date": "2026-02-01",
                                        "csv_path": "invoices/csv/legacy.csv"}})
        (folder / "csv").mkdir()
        (folder / "csv/legacy.csv").write_text("SCM;QTY\nLEGACY;1\n", encoding="utf-8")
        report = await self.reconcile()
        self.assertEqual(report["created"], 1)
        row = (await self.rows("SELECT invoice_date,currency,total_with_vat FROM uploaded_invoices"))[0]
        self.assertEqual(dict(row), {"invoice_date": None, "currency": None, "total_with_vat": None})

    async def test_exact_legacy_upload_is_adopted_without_losing_id_notes_or_payment_evidence(self):
        source = self.source(metadata={"invoice_number": "LEGACY-001", "currency": "EUR", "total_with_vat": "100.00"})
        original_path = self.data_root / "suppliers/archive-fixture/invoices/raw/legacy.pdf"
        original_path.parent.mkdir(parents=True)
        original_path.write_bytes(source["files"][0]["content"])
        async with self.sessions() as db:
            legacy_id = await db.scalar(text("""INSERT INTO uploaded_invoices
                (supplier_id,original_filename,stored_filename,file_path,invoice_number,currency,total_with_vat,
                 paid_amount,payment_status,notes) VALUES
                (:supplier,'legacy.pdf','legacy.pdf',:path,'LEGACY-001','EUR',100,25,'partial','Keep invoice note')
                RETURNING id"""), {"supplier": self.supplier_id, "path": str(original_path)})
            await db.commit()
        adopted = await self.ingest(source)
        self.assertEqual((adopted["id"], adopted["replayed"]), (legacy_id, True))
        row = (await self.rows("SELECT source_key,paid_amount,payment_status,notes FROM uploaded_invoices"))[0]
        self.assertEqual((row["source_key"], row["paid_amount"], row["payment_status"], row["notes"]),
                         ("document-001", D("25.00"), "partial", "Keep invoice note"))
        self.assertEqual(await self.scalar("SELECT count(*) FROM uploaded_invoices"), 1)
        self.assertEqual(await self.scalar("SELECT count(*) FROM invoice_payments"), 0)

    async def test_original_copy_survives_source_cache_change_and_symlinks_are_rejected(self):
        path = self.data_root / "suppliers/archive-fixture/invoices/raw/cache.pdf"
        path.parent.mkdir(parents=True)
        content = self.source()["files"][0]["content"]
        path.write_bytes(content)
        source = self.source(files=[{"path": path, "role": "original"}])
        result = await self.ingest(source)
        stored = Path(await self.scalar("SELECT file_path FROM invoice_files WHERE invoice_id=:id", {"id": result["id"]}))
        path.write_bytes(b"replaced external cache")
        self.assertEqual(stored.read_bytes(), content)
        with self.assertRaises(archive.InvoiceArchiveError):
            await self.ingest(source)
        target = self.data_root / "outside-private.txt"
        target.write_text("Synthetic private data", encoding="utf-8")
        linked = path.parent / "linked.txt"
        linked.symlink_to(target)
        self.index({"LINKED": {"number": "LINKED", "raw_path": "invoices/raw/linked.txt"}})
        report = await self.reconcile()
        self.assertEqual((report["created"], len(report["conflicts"])), (0, 1))
        self.assertEqual(await self.scalar("SELECT count(*) FROM uploaded_invoices"), 1)

    async def test_worker_completes_more_than_one_refresh_batch_without_duplicate_or_stock(self):
        from inventory_hub.services import invoice_archive_worker as worker
        folder = self.index([{'number': f'AUTO-{index:03}', 'csv_path': 'invoices/csv/shared.csv'} for index in range(205)])
        (folder / 'csv').mkdir()
        (folder / 'csv/shared.csv').write_text('SCM;QTY\nSYNTHETIC;1\n')

        @asynccontextmanager
        async def session_context():
            async with self.sessions() as db:
                yield db
                await db.commit()

        with patch.object(worker, 'get_session_context', session_context):
            for expected in (100, 200, 205, 205):
                await worker.process_supplier('archive-fixture')
                self.assertEqual(await self.scalar('SELECT count(*) FROM uploaded_invoices'), expected)
        self.assertEqual(worker.status(), {'items': []})
        self.assertEqual(await self.scalar('SELECT count(*) FROM uploaded_invoice_lines'), 205)
        await self.assert_no_inventory_effects()


@unittest.skipUnless(TEST_URL, DATABASE_REQUIRED)
class InvoiceUploadDatabaseTests(InvoiceDatabaseCase):
    """Both HTTP entry points share durable evidence and verified receiving files."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.content = b'%PDF-1.7\nSynthetic supplier invoice\n%%EOF'
        self.invoice = skp.parse_invoice_text(invoice_text())
        self.parser = self.enterContext(patch.object(skp, 'parse_invoice_pdf', return_value=self.invoice))
        async with self.sessions() as db:
            await db.execute(text("UPDATE suppliers SET code='sk-profi-bike' WHERE id=:id"), {'id': self.supplier_id})
            await db.execute(text("UPDATE suppliers SET code='northfinder' WHERE id=:id"), {'id': self.other_supplier_id})
            await db.commit()
        for code in ('sk-profi-bike', 'northfinder'):
            config = self.data_root / 'suppliers' / code / 'config.json'
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({'code': code, 'name': 'Synthetic supplier'}))
        app = FastAPI()
        app.include_router(routes.router)
        app.include_router(suppliers.router)
        async def session():
            async with self.sessions() as db:
                yield db
        app.dependency_overrides[get_session] = session
        app.dependency_overrides[operator_access] = lambda: 'synthetic-operator'
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url='http://invoice-fixture')
        self.addAsyncCleanup(self.client.aclose)

    async def upload(self, *, legacy=False, supplier='sk-profi-bike', filename='original.pdf', content=None, **fields):
        files = {'file': (filename, self.content if content is None else content)}
        if legacy:
            return await self.client.post(f'/suppliers/{supplier}/upload-invoice', files=files, params=fields)
        data = {'supplier_code': supplier, **fields} if supplier is not None else fields
        return await self.client.post('/invoices/upload', files=files, data=data)

    def index_path(self, supplier='sk-profi-bike'):
        return self.data_root / 'suppliers' / supplier / 'invoices/index.latest.json'

    async def assert_original(self, identifier, content):
        originals = await self.rows("SELECT * FROM invoice_files WHERE invoice_id=:id AND role='original'", {'id': identifier})
        self.assertEqual(len(originals), 1)
        self.assertEqual(originals[0]['sha256'], hashlib.sha256(content).hexdigest())
        self.assertEqual(Path(originals[0]['file_path']).read_bytes(), content)

    def spreadsheet(self):
        data = BytesIO()
        pd.DataFrame([{'Kód': '001', 'Názov': 'Synthetic Northfinder bonus', 'Množstvo': 2,
            'Jednotková cena v EUR': 0, 'Spolu v EUR': 0, 'Spolu v EUR s DPH': 0,
            'EAN': '0012345678905'}]).to_excel(data, index=False)
        return data.getvalue()

    async def test_auto_supplier_uses_validated_content_and_is_idempotent(self):
        northfinder = nf.parse_invoice_pages(invoice_pages())
        other_pdf = b'%PDF-1.7\nAnother synthetic supplier document\n%%EOF'

        def only(content, expected, invoice):
            if content != expected:
                raise nf.InvoiceParseError('Different issuer')
            return invoice

        with (patch.object(nf, 'parse_invoice_pdf', side_effect=lambda raw: only(raw, self.content, northfinder)),
              patch.object(skp, 'parse_invoice_pdf', side_effect=lambda raw: only(raw, other_pdf, self.invoice))):
            auto_nf = await self.upload(supplier=None, filename='misleading-sk-profi-bike.pdf')
            auto_skp = await self.upload(supplier=None, filename='misleading-northfinder.pdf', content=other_pdf)
            repeat = await self.upload(supplier=None, filename='renamed.pdf')
        for response, code, document, expected_content in (
                (auto_nf, 'northfinder', northfinder, self.content),
                (auto_skp, 'sk-profi-bike', self.invoice, other_pdf)):
            self.assertEqual(response.status_code, 200, response.text)
            saved = response.json()
            self.assertEqual((saved['supplier_code'], saved['invoice_number']), (code, document.number))
            self.assertTrue(saved['processing']['receiving_ready'])
            await self.assert_original(saved['id'], expected_content)
        self.assertEqual(repeat.status_code, 200, repeat.text)
        self.assertEqual(repeat.json()['id'], auto_nf.json()['id'])
        self.assertTrue(repeat.json()['replayed'])
        self.assertEqual(await self.scalar('SELECT count(*) FROM uploaded_invoices'), 2)
        await self.assert_no_inventory_effects()

    async def test_auto_supplier_requires_manual_selection_without_archiving_ambiguous_or_unknown_input(self):
        pdf = nf.parse_invoice_pages(invoice_pages())
        with patch.object(nf, 'parse_invoice_pdf', return_value=pdf):
            ambiguous = await self.upload(supplier=None)
        self.assertEqual(ambiguous.status_code, 422, ambiguous.text)
        self.assertEqual(ambiguous.json()['detail']['code'], 'invoice_supplier_ambiguous')
        with patch.object(skp, 'parse_invoice_pdf', side_effect=skp.InvoiceParseError('Unsupported issuer')):
            unknown = await self.upload(supplier=None)
        self.assertEqual(unknown.status_code, 422, unknown.text)
        self.assertEqual(unknown.json()['detail']['code'], 'invoice_supplier_required')
        scan = await self.upload(supplier=None, filename='scan.png', content=b'synthetic scan')
        self.assertEqual(scan.status_code, 422, scan.text)
        self.assertEqual(scan.json()['detail']['code'], 'invoice_supplier_required')
        self.assertEqual(await self.scalar('SELECT count(*) FROM uploaded_invoices'), 0)
        self.assertEqual(await self.scalar('SELECT count(*) FROM invoice_files'), 0)
        await self.assert_no_inventory_effects()

    async def test_canonical_then_legacy_reuses_original_identity_and_verified_receiving_csv(self):
        first = await self.upload()
        self.assertEqual(first.status_code, 200, first.text)
        saved = first.json()
        self.assertEqual(saved['archive'], {'status': 'completed', 'id': saved['id']})
        self.assertEqual(saved['processing']['status'], 'complete')
        self.assertTrue(saved['processing']['receiving_ready'])
        index_path = self.index_path()
        index = json.loads(index_path.read_text())
        entry = index['sk-profi-bike:9_26_00999']
        entry.update(status='processed', note='Preserve receiving note', current_session_id='123')
        index_path.write_text(json.dumps(index))
        before = index_path.read_bytes()
        replay = await self.upload(legacy=True, filename='renamed-original.pdf')
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual((replay.json()['id'], replay.json()['csv_path']), (saved['id'], saved['csv_path']))
        self.assertTrue(replay.json()['replayed'])
        self.assertEqual(index_path.read_bytes(), before)
        csv_path = self.data_root / saved['csv_path']
        self.assertEqual(csv_path.read_bytes(), self.invoice.csv_bytes())
        verified = receiving_source.load_source(self.data_root, 'sk-profi-bike', self.invoice.number,
            csv_path, receiving_db._parse_invoice_csv)
        self.assertEqual(len(verified.rows), 3)
        self.assertEqual(await self.scalar('SELECT count(*) FROM uploaded_invoices'), 1)
        self.assertEqual(await self.scalar('SELECT count(*) FROM uploaded_invoice_lines'), 3)
        self.assertFalse(await self.scalar('SELECT vat_included FROM uploaded_invoices'))
        await self.assert_original(saved['id'], self.content)
        await self.assert_no_inventory_effects()

    async def test_concurrent_entry_points_share_one_original_document_and_index(self):
        responses = await asyncio.gather(self.upload(), self.upload(legacy=True, filename='copy.pdf'))
        for response in responses:
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()['processing']['receiving_ready'])
        self.assertEqual(responses[0].json()['id'], responses[1].json()['id'])
        self.assertEqual(sorted(item.json()['replayed'] for item in responses), [False, True])
        self.assertEqual(await self.scalar('SELECT count(*) FROM uploaded_invoices'), 1)
        self.assertEqual(await self.scalar('SELECT count(*) FROM uploaded_invoice_lines'), 3)
        self.assertEqual(len(json.loads(self.index_path().read_text())), 1)
        await self.assert_original(responses[0].json()['id'], self.content)
        await self.assert_no_inventory_effects()

    async def test_malformed_pdf_retains_original_without_lines_index_or_stock_and_replays(self):
        self.parser.side_effect = skp.InvoiceParseError('Synthetic parser failure')
        first, replay = await self.upload(), await self.upload(legacy=True, filename='renamed.pdf')
        for response in (first, replay):
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['processing']['issues'], ['invoice_parse_failed'])
            self.assertFalse(response.json()['processing']['receiving_ready'])
            self.assertEqual(response.json()['processing']['stored_lines'], 0)
        self.assertEqual(first.json()['id'], replay.json()['id'])
        self.assertTrue(replay.json()['replayed'])
        self.assertEqual(await self.scalar('SELECT count(*) FROM uploaded_invoice_lines'), 0)
        self.assertIsNone(await self.scalar('SELECT total_with_vat FROM uploaded_invoices'))
        self.assertFalse(self.index_path().exists())
        await self.assert_original(first.json()['id'], self.content)
        await self.assert_no_inventory_effects()

    async def test_unverified_csv_and_foreign_spreadsheet_retain_original_without_receiving(self):
        for filename, content, issue, lines in (
            ('invoice.csv', b'SCM;QTY;PRICE\nA;2;12.34\n', 'receiving_source_unverified', 1),
            ('invoice.xlsx', self.spreadsheet(), 'invoice_parser_unavailable', 0),
        ):
            response = await self.upload(filename=filename, content=content)
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            self.assertEqual(result['processing']['issues'], [issue])
            self.assertEqual(result['processing']['stored_lines'], lines)
            self.assertFalse(result['processing']['receiving_ready'])
            await self.assert_original(result['id'], content)
        self.assertEqual(await self.scalar('SELECT count(*) FROM uploaded_invoices'), 2)
        self.assertFalse(self.index_path().exists())
        await self.assert_no_inventory_effects()

    async def test_changed_facts_or_original_cannot_replace_existing_evidence(self):
        first = await self.upload()
        self.assertEqual(first.status_code, 200, first.text)
        index_before = self.index_path().read_bytes()
        csv_path = self.data_root / first.json()['csv_path']
        csv_before = csv_path.read_bytes()
        self.parser.return_value = skp.parse_invoice_text(invoice_text().replace('Test product alpha', 'Changed product'))
        changed_facts = await self.upload(legacy=True)
        self.assertEqual(changed_facts.status_code, 409, changed_facts.text)
        self.assertEqual(changed_facts.json()['detail']['code'], 'invoice_source_changed')
        self.parser.return_value = self.invoice
        changed_original = await self.upload(content=self.content + b'\nChanged original')
        self.assertEqual(changed_original.status_code, 409, changed_original.text)
        self.assertEqual(self.index_path().read_bytes(), index_before)
        self.assertEqual(csv_path.read_bytes(), csv_before)
        self.assertEqual(await self.scalar('SELECT count(*) FROM uploaded_invoices'), 1)
        self.assertEqual(await self.scalar('SELECT count(*) FROM uploaded_invoice_lines'), 3)
        await self.assert_original(first.json()['id'], self.content)
        await self.assert_no_inventory_effects()

    async def test_receiving_preparation_conflict_still_commits_original_and_parsed_facts(self):
        raw = self.index_path().parent / 'pdf' / (self.invoice.key + '.pdf')
        raw.parent.mkdir(parents=True)
        previous = self.content + b'\nOther interrupted source'
        raw.write_bytes(previous)
        prior = skp.parse_invoice_text(invoice_text().replace('DEMO-A', 'OTHER-SOURCE'))
        self.parser.side_effect = lambda value: self.invoice if value == self.content else prior
        response = await self.upload()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['processing']['issues'], ['invoice_receiving_preparation_conflict'])
        self.assertFalse(response.json()['processing']['receiving_ready'])
        self.assertEqual(response.json()['processing']['stored_lines'], 3)
        self.assertEqual(raw.read_bytes(), previous)
        self.assertFalse(self.index_path().exists())
        self.assertEqual(await self.scalar('SELECT count(*) FROM uploaded_invoice_lines'), 3)
        await self.assert_original(response.json()['id'], self.content)
        await self.assert_no_inventory_effects()

    async def test_northfinder_xlsx_keeps_zero_cost_leading_zero_identity_and_verified_source(self):
        content = self.spreadsheet()
        response = await self.upload(supplier='northfinder', filename='Northfinder-1234567.xlsx', content=content)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result['processing']['receiving_ready'])
        self.assertEqual(result['invoice_number'], '1234567')
        self.assertIn('invoice_date', result['processing']['missing_fields'])
        line = (await self.rows('SELECT supplier_sku,ean,quantity,unit_price FROM uploaded_invoice_lines'))[0]
        self.assertEqual(dict(line), {'supplier_sku': '001', 'ean': '0012345678905', 'quantity': D(2), 'unit_price': D(0)})
        csv_path = self.data_root / result['csv_path']
        verified = receiving_source.load_source(self.data_root, 'northfinder', '1234567', csv_path, receiving_db._parse_invoice_csv)
        self.assertEqual((verified.adapter, verified.rows[0]['unit_price']), ('northfinder_xlsx', D(0)))
        self.assertFalse(await self.scalar('SELECT vat_included FROM uploaded_invoices'))
        self.assertIsNone(await self.scalar('SELECT total_with_vat FROM uploaded_invoices'))
        await self.assert_original(result['id'], content)
        csv_path.write_bytes(csv_path.read_bytes().replace(b';2;', b';3;'))
        with self.assertRaises(receiving_source.ReceivingSourceError):
            receiving_source.load_source(self.data_root, 'northfinder', '1234567', csv_path, receiving_db._parse_invoice_csv)
        await self.assert_no_inventory_effects()

    async def test_northfinder_pdf_keeps_archive_amounts_and_verified_goods_only(self):
        invoice = nf.parse_invoice_pages(invoice_pages())
        with patch.object(nf, 'parse_invoice_pdf', return_value=invoice):
            response = await self.upload(supplier='northfinder')
            self.assertEqual(response.status_code, 200, response.text)
            saved = response.json()
            self.assertEqual(saved['processing']['stored_lines'], 4)
            self.assertTrue(saved['processing']['receiving_ready'])
            self.assertFalse(await self.scalar('SELECT vat_included FROM uploaded_invoices'))
            rows = await self.rows('SELECT total_price FROM uploaded_invoice_lines ORDER BY line_number')
            self.assertEqual([row['total_price'] for row in rows], [D(36), D(24), D(5), D('-7.01')])
            await self.assert_original(saved['id'], self.content)
            csv_path = self.data_root / saved['csv_path']
            verified = receiving_source.load_source(self.data_root, 'northfinder', invoice.number, csv_path,
                                                    receiving_db._parse_invoice_csv)
            self.assertEqual((verified.adapter, [row['unit_price'] for row in verified.rows]),
                             ('northfinder_pdf', [D(18), D(24)]))
            async with self.sessions() as db:
                report = await archive.reconcile_supplier(db, InvoiceReconcileInput(supplier_code='northfinder'))
                await db.commit()
            self.assertEqual((report['existing'], report['conflicts']), (1, []))
            replay = await self.upload(legacy=True, supplier='northfinder')
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json()['id'], saved['id'])
            await self.assert_original(saved['id'], self.content)
            csv_path.write_bytes(csv_path.read_bytes().replace(b';18.00;', b';17.00;'))
            with self.assertRaises(receiving_source.ReceivingSourceError):
                receiving_source.load_source(self.data_root, 'northfinder', invoice.number, csv_path,
                                             receiving_db._parse_invoice_csv)
        await self.assert_no_inventory_effects()

    async def test_northfinder_pdf_completion_keeps_legacy_invoice_id_bytes_and_notes(self):
        invoice = nf.parse_invoice_pages(invoice_pages())
        with patch.object(nf, 'parse_invoice_pdf', side_effect=nf.InvoiceParseError('Old parser unavailable')):
            first = await self.upload(supplier='northfinder', filename='legacy-document.pdf')
        self.assertEqual(first.status_code, 200, first.text)
        identifier = first.json()['id']
        # A pre-migration upload can have the filename stem instead of a printed number.
        async with self.sessions() as db:
            await db.execute(text("UPDATE uploaded_invoices SET source_key=NULL,invoice_number='legacy-document',"
                                  "notes='Retain operator note' WHERE id=:id"), {'id': identifier})
            await db.commit()
        with patch.object(nf, 'parse_invoice_pdf', return_value=invoice):
            path = f'/invoices/{identifier}/reparse-northfinder-pdf'
            responses = await asyncio.gather(self.client.post(path), self.client.post(path))
            for response in responses:
                self.assertEqual(response.status_code, 200, response.text)
                saved = response.json()
                self.assertEqual(saved['id'], identifier)
                self.assertTrue(saved['processing']['receiving_ready'])
            async with self.sessions() as db:
                report = await archive.reconcile_supplier(db, InvoiceReconcileInput(supplier_code='northfinder'))
                await db.commit()
            self.assertEqual(report['conflicts'], [])
        row = (await self.rows('SELECT * FROM uploaded_invoices'))[0]
        self.assertEqual((row['id'], row['invoice_number'], row['notes'], row['items_count']),
                         (identifier, invoice.number, 'Retain operator note', 4))
        self.assertEqual(row['source_data']['pdf_parse_completion']['previous_number'], 'legacy-document')
        self.assertEqual(await self.scalar('SELECT count(*) FROM uploaded_invoices'), 1)
        self.assertEqual(await self.scalar('SELECT count(*) FROM uploaded_invoice_lines'), 4)
        await self.assert_original(identifier, self.content)
        await self.assert_no_inventory_effects()

    async def test_northfinder_pdf_completion_reconciles_older_supplier_original_and_retries(self):
        invoice = nf.parse_invoice_pages(invoice_pages())
        original = self.index_path('northfinder').parent / 'raw' / 'legacy-supplier-pdf.pdf'
        original.parent.mkdir(parents=True)
        original.write_bytes(self.content)
        upload_result = {'path': original.relative_to(self.data_root).as_posix(), 'filename': original.name}

        @asynccontextmanager
        async def session_context():
            async with self.sessions() as db:
                yield db
                await db.commit()

        with patch("inventory_hub.database.get_session_context", session_context), patch.object(
                archive.settings, "USE_POSTGRES", True), patch.object(nf, 'parse_invoice_pdf', return_value=invoice):
            archived = await archive.archive_after_upload('northfinder', upload_result, queued=True)
            self.assertEqual(archived['status'], 'completed')
            identifier = archived['id']
            self.assertEqual(archived['source_key'], original.stem)
            path = f'/invoices/{identifier}/reparse-northfinder-pdf'
            for action in ('archive', 'restore'):
                async with self.sessions() as db:
                    await db.execute(text('''INSERT INTO invoice_source_archive_events
                        (id,supplier_id,source_key,action,note,actor,request_hash)
                        VALUES(:id,:supplier,:key,:action,'Fixture lifecycle event','fixture',:hash)'''),
                        {'id': uuid4(), 'supplier': self.other_supplier_id, 'key': original.stem,
                         'action': action, 'hash': 'a' * 64})
                    await db.commit()
                response = await self.client.post(path)
                if action == 'archive':
                    self.assertEqual(response.status_code, 409, response.text)
                    self.assertEqual(response.json()['detail']['code'], 'invoice_source_archived')
                else:
                    self.assertEqual(response.status_code, 200, response.text)
                    saved = response.json()
                    self.assertEqual((saved['id'], saved['source_key']), (identifier, invoice.number))
                    self.assertTrue(saved['processing']['receiving_ready'])
            replay = await self.client.post(path)
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual((replay.json()['id'], replay.json()['source_key']), (identifier, invoice.number))
            async with self.sessions() as db:
                report = await archive.reconcile_supplier(db, InvoiceReconcileInput(supplier_code='northfinder'))
                await db.commit()
            self.assertEqual((report['created'], report['existing'], report['conflicts']), (0, 1, []))
            queue_replay = await archive.archive_after_upload('northfinder', upload_result, queued=True)
            self.assertEqual((queue_replay['status'], queue_replay['id'], queue_replay['replayed']),
                             ('completed', identifier, True))
            original.write_bytes(self.content + b'changed')
            conflict = await archive.archive_after_upload('northfinder', upload_result, queued=True)
            self.assertEqual(conflict, {'status': 'pending', 'code': 'invoice_source_changed'})
        row = (await self.rows('SELECT * FROM uploaded_invoices'))[0]
        self.assertEqual((row['source_kind'], row['source_key'], row['invoice_number'], row['items_count']),
                         ('supplier', invoice.number, invoice.number, 4))
        self.assertEqual(row['source_data']['pdf_parse_completion']['previous_source_key'], original.stem)
        self.assertEqual(await self.scalar('SELECT count(*) FROM uploaded_invoices'), 1)
        await self.assert_original(identifier, self.content)
        await self.assert_no_inventory_effects()

    async def test_northfinder_pdf_completion_refuses_changed_existing_facts(self):
        invoice = nf.parse_invoice_pages(invoice_pages())
        with patch.object(nf, 'parse_invoice_pdf', side_effect=nf.InvoiceParseError('Old parser unavailable')):
            first = await self.upload(supplier='northfinder')
        identifier = first.json()['id']
        async with self.sessions() as db:
            await db.execute(text("UPDATE uploaded_invoices SET currency='CZK' WHERE id=:id"), {'id': identifier})
            await db.commit()
        with patch.object(nf, 'parse_invoice_pdf', return_value=invoice):
            result = await self.client.post(f'/invoices/{identifier}/reparse-northfinder-pdf')
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(result.json()['detail']['code'], 'invoice_reparse_conflict')
        self.assertEqual(await self.scalar('SELECT currency FROM uploaded_invoices'), 'CZK')
        await self.assert_original(identifier, self.content)
        self.assertEqual(await self.scalar('SELECT count(*) FROM uploaded_invoice_lines'), 0)
        self.assertFalse(self.index_path('northfinder').exists())
        await self.assert_no_inventory_effects()

    async def test_northfinder_missing_number_is_rejected_before_archive_then_operator_can_retry(self):
        content = self.spreadsheet()
        for legacy in (False, True):
            response = await self.upload(legacy=legacy, supplier='northfinder', filename='original.xlsx', content=content)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()['detail']['code'], 'invoice_number_required_for_receiving')
        self.assertEqual(await self.scalar('SELECT count(*) FROM uploaded_invoices'), 0)
        self.assertEqual(await self.scalar('SELECT count(*) FROM invoice_files'), 0)
        self.assertFalse(self.index_path('northfinder').exists())
        first = await self.upload(supplier='northfinder', filename='original.xlsx', content=content, invoice_number='NF-001')
        replay = await self.upload(legacy=True, supplier='northfinder', filename='renamed.xlsx', content=content, invoice_number='NF-001')
        self.assertEqual((first.status_code, replay.status_code), (200, 200), (first.text, replay.text))
        self.assertEqual(first.json()['id'], replay.json()['id'])
        self.assertTrue(replay.json()['processing']['receiving_ready'])
        self.assertEqual(await self.scalar('SELECT count(*) FROM uploaded_invoices'), 1)
        await self.assert_original(first.json()['id'], content)
        await self.assert_no_inventory_effects()

    async def test_upload_reconcile_then_reupload_preserves_pdf_and_xlsx_identity_lines_and_notes(self):
        for supplier, filename, content, expected_lines in (
            ('sk-profi-bike', 'original.pdf', self.content, 3),
            ('northfinder', 'Northfinder-1234567.xlsx', self.spreadsheet(), 1),
        ):
            first = await self.upload(supplier=supplier, filename=filename, content=content)
            self.assertEqual(first.status_code, 200, first.text)
            identifier = first.json()['id']
            async with self.sessions() as db:
                await db.execute(text('UPDATE uploaded_invoices SET notes=:note WHERE id=:id'),
                    {'id': identifier, 'note': 'Keep verified operator note'})
                await db.commit()
            before_lines = await self.rows('SELECT * FROM uploaded_invoice_lines WHERE invoice_id=:id', {'id': identifier})
            for _ in range(2):
                async with self.sessions() as db:
                    report = await archive.reconcile_supplier(db, InvoiceReconcileInput(supplier_code=supplier))
                    await db.commit()
                self.assertEqual(report['conflicts'], [])
                self.assertEqual((report['created'], report['existing']), (0, 1))
            replay = await self.upload(legacy=True, supplier=supplier, filename=filename, content=content)
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json()['id'], identifier)
            self.assertTrue(replay.json()['replayed'])
            self.assertEqual(replay.json()['processing']['stored_lines'], expected_lines)
            self.assertEqual(await self.scalar('SELECT notes FROM uploaded_invoices WHERE id=:id', {'id': identifier}),
                'Keep verified operator note')
            self.assertEqual(await self.rows('SELECT * FROM uploaded_invoice_lines WHERE invoice_id=:id', {'id': identifier}), before_lines)
            await self.assert_original(identifier, content)
        self.assertEqual(await self.scalar('SELECT count(*) FROM uploaded_invoices'), 2)
        self.assertEqual(await self.scalar('SELECT count(*) FROM uploaded_invoice_lines'), 4)
        await self.assert_no_inventory_effects()

    async def test_retired_source_rejects_both_entry_points_before_creating_archive_or_index(self):
        async with self.sessions() as db:
            await db.execute(text('''INSERT INTO invoice_source_archive_events
                (id,supplier_id,source_key,action,note,actor,request_hash)
                VALUES(:id,:supplier,:key,'archive','Retire synthetic malformed source','fixture',:hash)'''),
                {'id': uuid4(), 'supplier': self.supplier_id, 'key': self.invoice.number, 'hash': 'a' * 64})
            await db.commit()
        for legacy in (False, True):
            response = await self.upload(legacy=legacy)
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()['detail']['code'], 'invoice_source_archived')
        self.assertEqual(await self.scalar('SELECT count(*) FROM uploaded_invoices'), 0)
        self.assertEqual(await self.scalar('SELECT count(*) FROM invoice_files'), 0)
        self.assertFalse(self.index_path().exists())
        await self.assert_no_inventory_effects()

    async def test_source_retired_after_archive_commit_cannot_prepare_receiving(self):
        original_lock = invoice_upload.lock_supplier
        lock_calls = 0

        async def retire_before_preparation(db, supplier_id):
            nonlocal lock_calls
            lock_calls += 1
            if lock_calls == 2:
                async with self.sessions() as retirement:
                    await retirement.execute(text('''INSERT INTO invoice_source_archive_events
                        (id,supplier_id,source_key,action,note,actor,request_hash)
                        VALUES(:id,:supplier,:key,'archive','Retire between upload phases','fixture',:hash)'''),
                        {'id': uuid4(), 'supplier': supplier_id, 'key': self.invoice.number, 'hash': 'b' * 64})
                    await retirement.commit()
            await original_lock(db, supplier_id)

        with patch.object(invoice_upload, 'lock_supplier', side_effect=retire_before_preparation):
            response = await self.upload()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(lock_calls, 2)
        self.assertEqual(result['processing']['status'], 'partial')
        self.assertEqual(result['processing']['issues'], ['invoice_source_archived'])
        self.assertFalse(result['processing']['receiving_ready'])
        self.assertEqual(await self.scalar('SELECT count(*) FROM uploaded_invoices'), 1)
        self.assertEqual(await self.scalar('SELECT count(*) FROM uploaded_invoice_lines'), 3)
        self.assertFalse(self.index_path().exists())
        await self.assert_original(result['id'], self.content)
        await self.assert_no_inventory_effects()

    async def test_archived_invoice_replay_retains_original_and_cannot_prepare_receiving(self):
        first = await self.upload()
        self.assertEqual(first.status_code, 200, first.text)
        identifier = first.json()['id']
        request = InvoiceProcessingPreview(action='archive', expected_revision=0, note='Retire synthetic invoice')
        async with self.sessions() as db:
            preview = await invoice_processing.preview(db, identifier, request)
            await invoice_processing.apply(db, identifier, InvoiceProcessingApply(**request.model_dump(),
                request_id=uuid4(), preview_hash=preview['preview_hash'], confirmed=True), actor='fixture')
            await db.commit()
        before = self.index_path().read_bytes()
        replay = await self.upload(legacy=True)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()['id'], identifier)
        self.assertTrue(replay.json()['replayed'])
        self.assertEqual(replay.json()['processing']['issues'], ['invoice_not_available_for_receiving'])
        self.assertFalse(replay.json()['processing']['receiving_ready'])
        self.assertEqual(self.index_path().read_bytes(), before)
        self.assertIsNotNone(await self.scalar('SELECT archived_at FROM uploaded_invoices'))
        await self.assert_original(identifier, self.content)
        await self.assert_no_inventory_effects()
