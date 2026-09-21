"""Configured feed downloads, independent of supplier parsers and shop APIs."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from inventory_hub import config_io
from inventory_hub.services import catalog


class CatalogDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch = patch.object(config_io, "DATA_ROOT", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.cfg = {"feeds": {"sources": {"products": {"mode": "remote", "remote": {"url": "https://supplier.example.test/feed?token=synthetic-secret"}}}}}
        path = config_io.supplier_path("northfinder")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(self.cfg))

    def test_download_preserves_bytes_without_parser_or_database(self):
        raw = b'<product><reference>NF-TEST</reference></product>\n<product/>'
        session = MagicMock()
        response = session.request.return_value.__enter__.return_value
        response.status_code = 200
        response.iter_content.return_value = [raw[:20], raw[20:]]
        with patch.object(requests, "Session") as factory:
            factory.return_value.__enter__.return_value = session
            result = catalog.download_catalog_source("northfinder")
        self.assertEqual((self.root / result["relpath"]).read_bytes(), raw)
        self.assertEqual(result["size_bytes"], len(raw))
        self.assertEqual(result["status"], "downloaded")
        self.assertTrue(session.request.call_args.kwargs["verify"])
        self.assertNotIn("synthetic-secret", str(result))
        with self.assertRaises(catalog.CatalogError) as error:
            catalog.source_config("northfinder", self.cfg, "products")
        self.assertEqual(error.exception.code, "catalog_parser_unavailable")

    def test_network_errors_are_specific_and_do_not_expose_credentials(self):
        for error, code in [(requests.exceptions.SSLError, "feed_tls_failed"),
                            (requests.exceptions.ConnectTimeout, "feed_timeout"),
                            (requests.exceptions.ReadTimeout, "feed_timeout"),
                            (requests.exceptions.ConnectionError, "feed_connection_failed")]:
            with self.subTest(code=code), patch.object(requests, "Session") as factory:
                factory.return_value.__enter__.return_value.request.side_effect = error("synthetic-secret")
                with self.assertRaises(catalog.CatalogError) as caught:
                    catalog.download_catalog_source("northfinder")
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("synthetic-secret", str(caught.exception))
        self.assertEqual(list(self.root.rglob("*.part")), [])

    def test_http_failure_and_empty_response_do_not_become_downloads(self):
        for status, code in [(401, "feed_auth_failed"), (403, "feed_auth_failed"), (404, "feed_not_found"),
                             (429, "feed_rate_limited"), (500, "feed_http_error"), (200, "feed_empty")]:
            with self.subTest(status=status), patch.object(requests, "Session") as factory:
                response = factory.return_value.__enter__.return_value.request.return_value.__enter__.return_value
                response.status_code = status
                response.iter_content.return_value = []
                with self.assertRaises(catalog.CatalogError) as caught:
                    catalog.download_catalog_source("northfinder")
                self.assertEqual(caught.exception.code, code)
        self.assertEqual(list(self.root.rglob("*.xml")), [])
        self.assertEqual(list(self.root.rglob("*.part")), [])

    def test_download_cannot_select_an_unconfigured_or_stock_source(self):
        for key in ("stock", "missing", "../products"):
            with self.subTest(key=key), patch.object(requests, "Session") as factory:
                with self.assertRaises(catalog.CatalogError):
                    catalog.download_catalog_source("northfinder", key)
                factory.assert_not_called()
