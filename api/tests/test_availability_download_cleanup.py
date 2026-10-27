"""Availability downloads must not accumulate or delete user-owned inputs."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from inventory_hub import config_io
from inventory_hub.services import catalog, supplier_availability_source as source


class AvailabilityDownloadCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch.object(config_io, "DATA_ROOT", self.root))
        self.directory = self.root / "suppliers/paul-lange/feeds/xml"
        self.directory.mkdir(parents=True)
        self.original = self.directory / "original.xml"
        self.original.write_text("<SHOP><SHOPITEM><ITEM_ID>TEST</ITEM_ID><STOCK>6+</STOCK></SHOPITEM></SHOP>")
        self.plan = {"supplier": "paul-lange", "feed_key": "stock", "parser": "paul-lange",
                     "source": {"mode": "local", "local_path": str(self.original)},
                     "config": {"feeds": {"sources": {"stock": {}}}}}

    def test_repeated_reads_keep_facts_and_original_without_disk_growth(self):
        before = self.original.read_bytes()
        for _ in range(3):
            rows, size = source.fetch(self.plan)
            self.assertEqual((rows[0].sku, rows[0].quantity_kind), ("TEST", "minimum"))
            self.assertEqual(size, len(before))
            self.assertEqual(list(self.directory.iterdir()), [self.original])
        self.assertEqual(self.original.read_bytes(), before)

    def test_structurally_invalid_feed_is_cleaned_and_error_preserved(self):
        self.original.write_text("<SHOP><SHOPITEM>")
        with self.assertRaises(source.AvailabilityError) as error:
            source.fetch(self.plan)
        self.assertEqual(error.exception.code, "supplier_availability_parse_failed")
        self.assertEqual(list(self.directory.iterdir()), [self.original])

    def test_empty_download_is_cleaned_and_error_preserved(self):
        self.original.write_bytes(b"")
        with self.assertRaises(source.AvailabilityError) as error:
            source.fetch(self.plan)
        self.assertEqual(error.exception.code, "feed_empty")
        self.assertEqual(list(self.directory.iterdir()), [self.original])

    def test_unexpected_parse_failure_also_removes_only_download(self):
        with patch.object(source, "parse", side_effect=RuntimeError("test")):
            with self.assertRaises(RuntimeError):
                source.fetch(self.plan)
        self.assertEqual(list(self.directory.iterdir()), [self.original])

    def test_listing_download_remains_persistent(self):
        path = catalog._download("paul-lange", self.plan["source"], "products")
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent, self.directory)
        self.assertEqual(path.read_bytes(), self.original.read_bytes())
