"""Real file route regressions; authentication itself is covered by access tests."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from inventory_hub import config_io
from inventory_hub.file_access import (
    PrivateDataFiles, document_path, local_feed_path, read_log_document,
)
from inventory_hub.settings import settings


class PrivateFileTests(unittest.TestCase):
    def setUp(self):
        self.folder = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.folder) / "data"
        self.root.mkdir()
        # Invoke the actual endpoint functions without starting workers or a DB.
        from inventory_hub import main
        self.enterContext(patch.object(settings, "INVENTORY_DATA_ROOT", self.root))
        self.enterContext(patch.object(config_io, "DATA_ROOT", self.root))
        self.app = FastAPI()
        self.app.mount("/data", PrivateDataFiles(directory=self.root))
        self.app.add_api_route("/files/download", main.files_download, methods=["GET", "HEAD"])
        self.app.add_api_route("/files/preview", main.preview_file, methods=["GET"])
        self.app.add_api_route("/files/stat", main.files_stat, methods=["GET"])
        self.client = TestClient(self.app)

    def write(self, relative, content="example"):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return target

    def test_invoice_feed_shop_export_and_history_keep_download_preview_and_head(self):
        paths = ["suppliers/skp/invoices/pdf/2026/invoice.pdf",
                 "suppliers/skp/invoices/raw/2026/invoice.xlsx",
                 "suppliers/skp/feeds/xml/products.xml",
                 "suppliers/skp/imports/upgates/export.csv",
                 "suppliers/skp/invoices/history/processed.json",
                 "shops/biketrek/latest.csv", "shops/xtrek/imports/upgates/new.csv"]
        for path in paths:
            self.write(path, "SKU;Qty\nSKP-1;2\n")
            with self.subTest(path=path):
                response = self.client.get("/files/download", params={"relpath": path})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], "private, no-store")
                self.assertEqual(self.client.head("/files/download", params={"relpath": path}).status_code, 200)
                self.assertEqual(self.client.get("/files/stat", params={"relpath": path}).status_code, 200)
                self.assertEqual(self.client.get("/data/" + path).status_code, 200)
        preview = self.client.get("/files/preview", params={"relpath": "shops/biketrek/latest.csv"})
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["rows"], [["SKP-1", "2"]])

    def test_private_files_cannot_be_downloaded_statted_previewed_or_served_static(self):
        paths = ["suppliers/skp/config.json", "suppliers/skp/state/storage_state.json",
                 "shops/biketrek/config.json", "suppliers/skp/config_history/old.json",
                 "suppliers/skp/feeds/xml/.env", "suppliers/skp/logs/cookies.json",
                 "suppliers/skp/logs/session.json", "suppliers/skp/logs/session_backup.json",
                 "suppliers/skp/logs/cookies-old.json", "suppliers/skp/logs/auth.json",
                 "suppliers/skp/logs/network.json", "suppliers/skp/feeds/xml/config.csv",
                 "suppliers/skp/feeds/xml/state/source.csv", "database.sqlite"]
        for path in paths:
            self.write(path, "synthetic-secret-never-returned")
            for route in ("/files/download", "/files/stat", "/files/preview"):
                with self.subTest(path=path, route=route):
                    response = self.client.get(route, params={"relpath": path})
                    self.assertEqual(response.status_code, 404)
                    self.assertNotIn("synthetic-secret", response.text)
            self.assertEqual(self.client.get("/data/" + path).status_code, 404)

    def test_traversal_absolute_paths_and_sibling_prefix_are_rejected(self):
        outside = Path(self.folder) / "data-private" / "secret.csv"
        outside.parent.mkdir()
        outside.write_text("secret")
        for value in (str(outside), "../data-private/secret.csv", "%2e%2e/data-private/secret.csv",
                      "suppliers/skp/feeds/xml/../../../../config.csv", "C:\\private\\secret.csv"):
            for route in ("/files/download", "/files/stat", "/files/preview"):
                with self.subTest(path=value, route=route):
                    self.assertIn(self.client.get(route, params={"relpath": value}).status_code, (400, 404))

    def test_file_and_directory_symlinks_are_not_document_aliases(self):
        private = self.write("suppliers/skp/config.json", "secret")
        location = self.root / "suppliers/skp/feeds/xml"
        location.mkdir(parents=True)
        (location / "disguised.csv").symlink_to(private)
        outside = Path(self.folder) / "outside"
        outside.mkdir()
        (outside / "source.csv").write_text("secret")
        (location / "outside").symlink_to(outside, target_is_directory=True)
        for path in ("suppliers/skp/feeds/xml/disguised.csv", "suppliers/skp/feeds/xml/outside/source.csv"):
            for route in ("/files/download", "/files/stat", "/files/preview"):
                self.assertEqual(self.client.get(route, params={"relpath": path}).status_code, 404)
            self.assertEqual(self.client.get("/data/" + path).status_code, 404)

    def test_html_xml_and_json_never_execute_inline_and_filenames_cannot_inject_headers(self):
        for suffix in ("html", "xml", "json"):
            path = f"suppliers/skp/logs/report.{suffix}"
            self.write(path, "<script>fetch('/api/private')</script>")
            for route in ("/files/download", "/data/" + path):
                response = self.client.get(route, params={"relpath": path, "disposition": "inline",
                    "filename": 'bad\"\r\nX-Injected: yes.html'})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.headers["content-type"].startswith("text/plain"))
                self.assertTrue(response.headers["content-disposition"].startswith("attachment;"))
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertIn("sandbox", response.headers["content-security-policy"])
                self.assertNotIn("x-injected", response.headers)
                self.assertNotIn("\r", response.headers["content-disposition"])

    def test_safe_images_still_render_inline_without_a_public_data_wildcard(self):
        path = "suppliers/skp/logs/screenshot.png"
        self.write(path)
        response = self.client.get("/data/" + path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertTrue(response.headers["content-disposition"].startswith("inline;"))
        self.assertEqual(self.client.get("/data/suppliers/skp/logs/").status_code, 404)

    def test_log_reader_is_supplier_scoped_bounded_and_returns_inert_html(self):
        path = "suppliers/skp/logs/report.html"
        self.write(path, "<script>unsafe()</script>")
        result = read_log_document(self.root, path, supplier="skp", max_bytes=8)
        self.assertEqual(result["text"], "<script>")
        self.assertTrue(result["truncated"])
        self.assertFalse(result["is_html"])
        for invalid in ("suppliers/skp/config.json", "suppliers/skp/feeds/xml/report.csv"):
            self.write(invalid)
            with self.assertRaises(HTTPException):
                read_log_document(self.root, invalid)
        with self.assertRaises(HTTPException):
            read_log_document(self.root, path, supplier="another")

    def test_legacy_prepare_rejects_arbitrary_local_reads_before_adapter(self):
        from inventory_hub import main
        from inventory_hub.models import RunPrepareIn
        good = self.write("suppliers/paul-lange/invoices/csv/invoice.csv", "SKU;Qty\nPL-1;1")
        with patch.object(main.paul_lange_v1, "process_invoice") as adapter:
            for invoice, override in (("../secret.csv", None), (str(good), "/etc/passwd"),
                                      (good.relative_to(self.root).as_posix(), "/etc/passwd")):
                with self.assertRaises(HTTPException):
                    main.prepare_run(RunPrepareIn(supplier_ref="paul-lange", shop_ref="biketrek",
                        invoice_relpath=invoice, upgates_csv_override=override))
            adapter.assert_not_called()

    def test_configured_local_feed_cannot_repackage_config_or_outside_files(self):
        from inventory_hub.services import catalog
        secret = self.write("suppliers/skp/config.json", '{"password":"synthetic-secret"}')
        good = self.write("suppliers/skp/feeds/xml/own.xml", "<SHOP/>")
        for path in (secret, Path("/etc/passwd")):
            with self.subTest(path=path), self.assertRaises(catalog.CatalogError):
                catalog._download("skp", {"mode": "local", "local_path": str(path)}, "products")
        result = catalog._download("skp", {"mode": "local", "local_path": "feeds/xml/own.xml"}, "products")
        self.assertEqual(result.read_text(), good.read_text())
        self.assertFalse(any("synthetic-secret" in f.read_text() for f in good.parent.glob("catalog_*.xml")))


    def test_import_send_rejects_secrets_cross_supplier_symlinks_and_mixed_batch_before_copy(self):
        from inventory_hub.routers import imports
        allowed = self.write("suppliers/skp/imports/upgates_selected/INV_selected.csv", "SKU;QTY\nSKP-1;1")
        private = self.write("suppliers/skp/config.json", "secret")
        other = self.write("suppliers/other/imports/upgates_selected/INV.csv", "other")
        feed = self.write("suppliers/skp/feeds/converted/source.csv", "feed")
        alias = allowed.parent / "alias.csv"
        alias.symlink_to(private)
        for bad in (private, other, feed, alias, Path("/etc/passwd")):
            with self.subTest(path=bad), self.assertRaises(HTTPException):
                imports.send_imports("skp", {"invoice_id": "skp:INV", "selected_files": [str(allowed), str(bad)]})
            self.assertFalse((self.root / "suppliers/skp/imports/sent").exists())
        result = imports.send_imports("skp", {"invoice_id": "skp:INV", "selected_files": [str(allowed)]})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(Path(result["sent"][0]["sent_file"]).read_text(), allowed.read_text())

    def test_import_apply_rejects_invoice_globs_and_traversal_before_processing(self):
        from inventory_hub.routers import imports
        for invoice in ("../escape", "skp:../../escape", "*", "[invoice]", "%2e%2e", "other:INV"):
            with self.subTest(invoice=invoice), self.assertRaises(HTTPException):
                imports.apply_imports("skp", {"invoice_id": invoice, "selected_product_codes": ["SKP-1"]})
        source = self.write("suppliers/skp/imports/upgates/INV_updates_existing_20261014.csv", "PRODUCT_CODE;QTY\nSKP-1;2\n")
        result = imports.apply_imports("skp", {"invoice_id": "skp:INV", "selected_product_codes": ["SKP-1"]})
        self.assertIn("SKP-1", Path(result["selected_files"]["updates"]).read_text())
        self.assertTrue(source.exists())

    def test_active_prepare_accepts_supplier_relative_csv_but_rejects_arbitrary_read(self):
        from inventory_hub.routers import invoices
        self.write("suppliers/skp/invoices/csv/INV.csv", "SCM;QTY\nSKU;1")
        self.write("shops/biketrek/latest.csv", "PRODUCT_CODE;STOCK\nSKU;0")
        self.write("suppliers/skp/config.json", "secret")
        defaults = {"supplier_ref": "skp", "shop_ref": "biketrek", "invoice_relpath": "invoices/csv/INV.csv"}
        invalid = [{"supplier_ref": "../skp"}, {"shop_ref": "../biketrek"},
                   {"invoice_relpath": "/etc/passwd"}, {"invoice_relpath": "../../shops/biketrek/latest.csv"},
                   {"invoice_relpath": "config.json"}, {"invoice_relpath": "invoices/csv/../INV.csv"}]
        with patch.object(invoices, "prepare_from_invoice", return_value={"outputs": {}}) as adapter:
            for change in invalid:
                with self.subTest(change=change), self.assertRaises(HTTPException):
                    invoices.run_prepare({**defaults, **change})
            adapter.assert_not_called()
            self.assertTrue(invoices.run_prepare(defaults)["ok"])
            self.assertEqual(adapter.call_args.args[3], "invoices/csv/INV.csv")


class LocalFeedSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_mapping_source_checks_uploaded_saved_and_configured_paths(self):
        from inventory_hub.services import catalog, feed_mapping
        with tempfile.TemporaryDirectory() as directory, patch.object(config_io, "DATA_ROOT", Path(directory)):
            root = Path(directory)
            good = root / "suppliers/skp/feeds/xml/source.xml"
            good.parent.mkdir(parents=True)
            good.write_text("<SHOP/>")
            cfg = {"feeds": {"sources": {"products": {"mode": "local", "local_path": "feeds/xml/source.xml"}}}}
            with patch.object(catalog, "supplier_config", return_value=cfg), patch.object(catalog, "_feed", new=AsyncMock(return_value=None)):
                self.assertEqual(await feed_mapping.source_path(None, "skp", "products"), good)
                cfg["feeds"]["sources"]["products"]["local_path"] = "/etc/passwd"
                with self.assertRaises(catalog.CatalogError):
                    await feed_mapping.source_path(None, "skp", "products")
            private = root / "suppliers/skp/config.json"
            private.write_text("secret")
            feed = SimpleNamespace(mapping_config={"mapping_source_path": "suppliers/skp/config.json"})
            with patch.object(catalog, "supplier_config", return_value=cfg), patch.object(catalog, "_feed", new=AsyncMock(return_value=feed)):
                with self.assertRaises(catalog.CatalogError):
                    await feed_mapping.source_path(None, "skp", "products")
            sample = feed_mapping.upload_path("skp", "products", "a" * 32)
            sample.parent.mkdir(parents=True)
            sample.write_text("SKU;Name\n1;Test")
            self.assertEqual(local_feed_path(root, sample), sample)
            sample.unlink()
            sample.symlink_to(private)
            with self.assertRaises(HTTPException):
                local_feed_path(root, sample)
