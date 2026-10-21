"""Old CSV preparation cannot bypass reviewed new-card identity in the Hub."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from inventory_hub.routers import invoices
from inventory_hub.services.legacy_import_guard import require_legacy_import_mode
from inventory_hub.settings import settings


class LegacySkuGuardTests(unittest.TestCase):
    def test_postgres_refuses_csv_preparation_before_adapter_or_index_write(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(settings, "INVENTORY_DATA_ROOT", Path(folder)), \
                patch.object(settings, "USE_POSTGRES", True), patch.object(invoices, "prepare_from_invoice") as adapter:
            root = Path(folder)
            invoice = root / "suppliers/test/invoices/csv/INV.csv"
            invoice.parent.mkdir(parents=True)
            invoice.write_text("SCM;QTY\n001;1\n")
            shop = root / "shops/test/latest.csv"
            shop.parent.mkdir(parents=True)
            shop.write_text("PRODUCT_CODE;STOCK\nTEST-001;0\n")
            before = sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())
            with self.assertRaises(HTTPException) as error:
                invoices.run_prepare({"supplier_ref": "test", "shop_ref": "test",
                                      "invoice_relpath": "invoices/csv/INV.csv"})
            self.assertEqual(error.exception.status_code, 409)
            self.assertEqual(error.exception.detail["code"], "sku_preparation_required")
            adapter.assert_not_called()
            self.assertEqual(before, sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()))

    def test_historical_json_mode_remains_available(self):
        with patch.object(settings, "USE_POSTGRES", False):
            self.assertIsNone(require_legacy_import_mode())
