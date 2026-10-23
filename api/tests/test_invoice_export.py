"""Original bytes, complete exports and private-path boundaries for accountants."""
import csv
import hashlib
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from inventory_hub.routers.invoice_export import router
from inventory_hub.services.invoice_export import make_bundle
from inventory_hub.settings import settings


class InvoiceExportTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.original = self.root / 'suppliers/test/invoices/raw/original.pdf'
        self.original.parent.mkdir(parents=True)
        self.content = b'%PDF-1.4\noriginal supplier bytes\n'
        self.original.write_bytes(self.content)
        self.document = {'id': 7,'direction': 'purchase','file_path': str(self.original),
                         'original_filename': '../originál.pdf','invoice_number': '=HYPERLINK("bad")',
                         'currency': 'CZK','total_with_vat': '0.00','paid_amount': '0.00',
                         'sha256': hashlib.sha256(self.content).hexdigest()}

    def test_original_bytes_and_safe_manifest_are_preserved(self):
        with make_bundle([self.document], self.root) as file, ZipFile(file) as archive:
            self.assertEqual(set(archive.namelist()), {'7/originál.pdf','manifest.csv'})
            self.assertEqual(archive.read('7/originál.pdf'), self.content)
            rows = list(csv.DictReader(io.StringIO(archive.read('manifest.csv').decode('utf-8-sig')), delimiter=';'))
            self.assertEqual(rows[0]['currency'], 'CZK')
            self.assertEqual(rows[0]['total_with_vat'], '0.00')
            self.assertTrue(rows[0]['invoice_number'].startswith("'="))
            self.assertEqual(rows[0]['sha256'], self.document['sha256'])

    def test_missing_private_or_changed_file_rejects_whole_bundle(self):
        private = self.root / 'suppliers/test/config.json'
        private.write_text('private')
        for changes, code in [({'file_path': str(private)}, 'invoice_export_file_missing'),
                              ({'file_path': str(self.original) + '.missing'}, 'invoice_export_file_missing'),
                              ({'sha256': '0' * 64}, 'invoice_export_file_changed')]:
            with self.subTest(changes=changes), self.assertRaises(HTTPException) as error:
                make_bundle([self.document, {**self.document, **changes}], self.root)
            self.assertEqual(error.exception.detail['code'], code)

    def test_large_file_rejected_before_download(self):
        with patch('inventory_hub.services.invoice_export.MAX_FILE_BYTES', 2), self.assertRaises(HTTPException) as error:
            make_bundle([self.document], self.root)
        self.assertEqual(error.exception.status_code, 413)

    def test_export_requires_operator_and_has_bounded_input(self):
        app = FastAPI(); app.include_router(router)
        from pydantic import SecretStr
        with patch.object(settings, 'AI_CONTENT_ACCESS_TOKEN', SecretStr('synthetic-export-token-1234567890')), TestClient(app) as client:
            self.assertEqual(client.post('/invoices/export', json={'invoice_ids':[7]}).status_code, 401)
            response = client.post('/invoices/export', headers={'Authorization':'Bearer synthetic-export-token-1234567890'}, json={'invoice_ids': []})
            self.assertEqual(response.status_code, 422)
            self.assertIn('no-store', response.headers['Cache-Control'])
