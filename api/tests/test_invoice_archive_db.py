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

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from fastapi import UploadFile

from invoice_db_fixture import DATABASE_REQUIRED, TEST_URL, InvoiceDatabaseCase
from inventory_hub.invoice_archive_types import InvoiceReconcileInput
from inventory_hub.routers import invoices_unified as routes
from inventory_hub.services import invoice_archive as archive
from inventory_hub.services import invoice_export


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
