"""Exercise host cleanup boundaries against an isolated synthetic file tree."""
import importlib.util
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

spec = importlib.util.spec_from_file_location("availability_xml_prune", Path(__file__).resolve().parents[2] / "scripts/prune_availability_xml.py")
prune = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = prune
spec.loader.exec_module(prune)


class AvailabilityXMLPruneTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "suppliers/synthetic/feeds/xml"
        self.directory.mkdir(parents=True)
        self.config = self.root / "suppliers/synthetic/config.json"
        self.config.write_text("{}")
        self.at = 2_000_000_000

    def write(self, name, hours):
        path = self.directory / name
        path.write_text("synthetic")
        os.utime(path, (self.at - hours * 3600, self.at - hours * 3600))
        return path

    def copies(self):
        return [self.write(f"catalog_stock_20261020T120000_{i:08x}.xml", hours)
                for i, hours in enumerate((1, 2, 72, 73, 100, 200))]

    def select(self):
        return prune.candidates(self.root, "synthetic", 72, at=self.at)

    def test_preview_is_read_only_and_apply_preserves_recent_and_other_files(self):
        copies = self.copies()
        others = [self.write(name, 300) for name in (
            "catalog_products_20261020T120000_00000001.xml", "catalog_stock_bad.xml", "invoices.xml", "download.part")]
        selected = self.select()
        self.assertEqual({item.path for item in selected}, set(copies[3:]))
        self.assertTrue(all(path.exists() for path in copies))
        self.assertEqual(prune.remove_selected(selected)[0], 3)
        self.assertTrue(all(path.exists() for path in copies[:3] + others))

    def test_preserves_two_newest_even_when_all_are_old(self):
        copies = self.copies()
        selected = prune.candidates(self.root, "synthetic", 72, at=self.at + 300 * 3600)
        self.assertEqual({item.path for item in selected}, set(copies[2:]))

    def test_local_sources_and_container_paths_in_any_supplier_are_protected(self):
        copies = self.copies()
        other = self.root / "suppliers/other"
        other.mkdir()
        self.config.write_text(json.dumps({"feeds": {"sources": {"stock": {"local_path": "feeds/xml/" + copies[3].name}}}}))
        (other / "config.json").write_text(json.dumps({"feeds": {"sources": {"stock": {
            "local_path": "/data/suppliers/synthetic/feeds/xml/" + copies[4].name}}}}))
        self.assertEqual([item.path for item in self.select()], [copies[5]])

    def test_invalid_config_aborts_and_symlink_file_is_untouched(self):
        copies = self.copies()
        copies[3].unlink()
        copies[3].symlink_to(copies[4])
        self.assertNotIn(copies[3], [item.path for item in self.select()])
        self.config.write_text("invalid")
        with self.assertRaises(ValueError):
            self.select()
        self.assertTrue(copies[4].exists())

    def test_changed_candidate_is_not_deleted(self):
        self.copies()
        selected = self.select()
        selected[0].path.write_text("changed after preview")
        with self.assertRaises(RuntimeError):
            prune.remove_selected(selected)
        self.assertTrue(all(item.path.exists() for item in selected))

    def test_unsafe_scope_or_short_retention_is_rejected(self):
        for supplier, hours in (("../synthetic", 72), ("synthetic", 0)):
            with self.assertRaises(ValueError):
                prune.candidates(self.root, supplier, hours)
