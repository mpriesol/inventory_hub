"""Legacy receiving still obeys the same prefix identity contract as PostgreSQL."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from inventory_hub import config_io
from inventory_hub.routers.receiving import _product_code_for_supplier
from inventory_hub.supplier_prefix import SupplierPrefixError


class LegacyReceivingPrefixTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(config_io, "DATA_ROOT", Path(folder)))

    def test_first_persistent_code_preserves_zeros_and_locks_prefix(self):
        config_io.save_supplier("synthetic", {"product_code_prefix": "TEST-"})
        self.assertFalse(config_io.supplier_prefix_status("synthetic")["product_prefix_locked"])
        self.assertEqual(_product_code_for_supplier("synthetic", "00123"), "TEST-00123")
        self.assertTrue(config_io.supplier_prefix_status("synthetic")["product_prefix_locked"])
        with self.assertRaises(SupplierPrefixError):
            config_io.save_supplier("synthetic", {"product_code_prefix": "CHANGED-"})

    def test_missing_configuration_does_not_invent_paul_lange_prefix(self):
        with self.assertRaises(HTTPException) as error:
            _product_code_for_supplier("paul-lange", "00123")
        self.assertEqual(error.exception.detail["code"], "supplier_not_found")
