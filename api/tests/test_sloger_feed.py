"""All-or-nothing Sloger brand downloads through the ordinary catalog downloader."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from lxml import etree
import requests

from inventory_hub import config_io
from inventory_hub.adapters.sloger_catalog import parse_catalog
from inventory_hub.adapters.sloger_feed import SlogerFeedError, aggregate_catalog_sources
from inventory_hub.services import catalog


def fixture(code="SKU", brand="First"):
    return (f'<products><product><id>1</id><code>{code}</code><name>Product</name>'
            f'<manufacturer>{brand}</manufacturer><price_msrp>12</price_msrp>'
            '<images/><options/></product></products>').encode()


class SlogerFeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.destination = self.root / "catalog.xml"
        self.source = {"mode": "remote", "catalog_parser": "sloger", "remote": {
            "urls": ["https://supplier.test/first.xml?token=synthetic-secret", "https://supplier.test/second.xml"],
            "verify_ssl": True,
        }}

    def downloader(self, contents):
        def download(source, index, size_budget, time_budget):
            snapshot = self.root / f"source-{index}.xml"
            snapshot.write_bytes(contents[index - 1])
            return snapshot
        return MagicMock(side_effect=download)

    def test_combines_all_brands_and_retains_raw_snapshots_for_global_collision_check(self):
        contents = [fixture(brand="Force"), fixture(brand="Sigma")]
        download = self.downloader(contents)
        result = aggregate_catalog_sources(self.source, self.destination, download)
        self.assertEqual(result, self.destination)
        self.assertEqual(len(etree.parse(str(result)).findall("product")), 2)
        records = parse_catalog(result, "sloger", {})
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0][0].import_blockers, ["duplicate_supplier_code"])
        for index, content in enumerate(contents, 1):
            self.assertEqual((self.root / f"source-{index}.xml").read_bytes(), content)
        first, second = download.call_args_list
        self.assertNotIn("urls", first.args[0]["remote"])
        self.assertEqual(first.args[0]["remote"]["url"], self.source["remote"]["urls"][0])
        self.assertEqual(second.args[2], first.args[2] - len(contents[0]))
        self.assertEqual(len(self.source["remote"]["urls"]), 2)
        self.assertNotIn("synthetic-secret", result.read_text())
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_failed_second_download_preserves_prior_catalog_and_first_snapshot(self):
        self.destination.write_bytes(b"previous successful catalog")
        first = self.root / "source-1.xml"
        first.write_bytes(fixture())
        failure = catalog.CatalogError("feed_not_found", "Feed download returned HTTP 404", 502)
        with self.assertRaises(catalog.CatalogError) as caught:
            aggregate_catalog_sources(self.source, self.destination, MagicMock(side_effect=[first, failure]))
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.destination.read_bytes(), b"previous successful catalog")
        self.assertTrue(first.is_file())
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_empty_malformed_wrong_root_and_dtd_sources_abort_without_replacing_catalog(self):
        for invalid, code in [(b"<products/>", "feed_empty"),
                              (b"<products><product>", "feed_xml_invalid"),
                              (b"<html>Login required</html>", "feed_xml_invalid"),
                              (b"<products><error/></products>", "feed_xml_invalid"),
                              (b'<!DOCTYPE products [<!ENTITY secret SYSTEM "file:///synthetic-secret">]><products><product>&secret;</product></products>', "feed_xml_invalid")]:
            with self.subTest(code=code, invalid=invalid):
                self.destination.write_bytes(b"previous")
                download = self.downloader([fixture(), invalid])
                with self.assertRaises(SlogerFeedError) as caught:
                    aggregate_catalog_sources(self.source, self.destination, download)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("synthetic-secret", str(caught.exception))
                self.assertEqual(self.destination.read_bytes(), b"previous")
                self.assertEqual(list(self.root.glob("*.part")), [])

    def test_bad_or_repeated_url_list_is_rejected_before_download(self):
        for urls in ([], "https://supplier.test/feed.xml", [None], ["file:///secret"],
                     ["https://supplier.test/feed.xml"] * 2, ["https://supplier.test/feed.xml"] * 101):
            with self.subTest(urls=urls):
                source = {"remote": {"urls": urls}}
                download = MagicMock()
                with self.assertRaises(SlogerFeedError) as caught:
                    aggregate_catalog_sources(source, self.destination, download)
                self.assertEqual(caught.exception.code, "feed_urls_invalid")
                download.assert_not_called()

    def test_cumulative_download_budget_and_whole_refresh_deadline(self):
        padded = b"<products>" + b" " * 700 + fixture().split(b"<products>", 1)[1]
        with self.assertRaises(SlogerFeedError) as caught:
            aggregate_catalog_sources(self.source, self.destination, self.downloader([padded, padded]), max_size_bytes=1000)
        self.assertEqual(caught.exception.code, "feed_too_large")
        self.assertFalse(self.destination.exists())
        with patch("inventory_hub.adapters.sloger_feed.time.monotonic", side_effect=[0, 0, 2]):
            with self.assertRaises(SlogerFeedError) as caught:
                aggregate_catalog_sources(self.source, self.destination, self.downloader([fixture(), fixture()]), max_elapsed_seconds=1)
        self.assertEqual(caught.exception.code, "feed_timeout")
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_catalog_dispatch_reuses_http_downloader_and_preserves_three_snapshots(self):
        contents = [fixture("FIRST"), fixture("SECOND")]
        session = MagicMock()
        responses = []
        for raw in contents:
            response = MagicMock()
            response.__enter__.return_value.status_code = 200
            response.__enter__.return_value.iter_content.return_value = [raw]
            responses.append(response)
        session.request.side_effect = responses
        with patch.object(config_io, "DATA_ROOT", self.root), patch.object(requests, "Session") as factory:
            factory.return_value.__enter__.return_value = session
            result = catalog._download("sloger", self.source, "products")
        self.assertEqual(session.request.call_count, 2)
        self.assertEqual(len(list(self.root.rglob("*.xml"))), 3)
        self.assertEqual(len(etree.parse(str(result)).findall("product")), 2)
        self.assertTrue(catalog._source_configured(self.source))
        self.assertNotIn("synthetic-secret", result.read_text())

    def test_other_parsers_cannot_silently_ignore_multiple_urls(self):
        source = {**self.source, "catalog_parser": "northfinder"}
        with patch.object(config_io, "DATA_ROOT", self.root), patch.object(requests, "Session") as factory:
            with self.assertRaises(catalog.CatalogError) as caught:
                catalog._download("northfinder", source, "products")
        self.assertEqual(caught.exception.code, "feed_urls_unsupported")
        factory.assert_not_called()
