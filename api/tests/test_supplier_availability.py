"""Supplier configuration round-trips use temporary files, never live settings."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from inventory_hub import config_io
from inventory_hub.config_normalize import normalize_supplier_config
from inventory_hub.routers.suppliers import put_supplier_config
from inventory_hub.services.catalog_merchandising import availability_policy


class SupplierAvailabilityConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.patch = patch.object(config_io, 'DATA_ROOT', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_legacy_config_gets_persisted_defaults_without_mutating_input(self):
        raw = config_io._norm_supplier({'name': 'Synthetic supplier', 'adapter_settings': {'price_coefficients': {'TEST': 1}}})
        raw['adapter_settings'].pop('availability')
        before = copy.deepcopy(raw)
        normalized = config_io._norm_supplier(raw)
        self.assertEqual(raw, before)
        path = config_io.supplier_path('synthetic')
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(raw), encoding='utf-8')
        config_io.load_supplier('synthetic', write_back_on_load=False)
        self.assertEqual(json.loads(path.read_text()), raw)
        config_io.load_supplier('synthetic', write_back_on_load=True)
        self.assertEqual(json.loads(path.read_text()), normalized)
        self.assertEqual(normalized['adapter_settings']['availability'], {'orderable': 'do 5 dní', 'unknown': 'overíme'})

    def test_save_load_preserves_supplier_labels_and_unrelated_nested_extensions(self):
        raw = {'adapter_settings': {'availability': {'orderable': ' do 7 dní ', 'unknown': ' ', 'retained_extension': {'custom': True}},
                                    'price_coefficients': {'TEST': 1}}}
        saved = config_io.save_supplier('synthetic', raw)
        loaded = config_io.load_supplier('synthetic', write_back_on_load=False)
        self.assertEqual(saved, loaded)
        self.assertEqual(loaded['adapter_settings']['availability'], {'orderable': 'do 7 dní', 'unknown': 'overíme', 'retained_extension': {'custom': True}})
        self.assertEqual(loaded['adapter_settings']['price_coefficients'], {'TEST': 1})
        self.assertEqual(normalize_supplier_config(raw)['adapter_settings']['availability'], loaded['adapter_settings']['availability'])
        self.assertNotIn('retained_extension', availability_policy('synthetic', loaded))

    def test_invalid_labels_are_rejected_before_any_config_write(self):
        for availability in ([], {'orderable': 7}, {'unknown': 'x' * 101}):
            with self.subTest(availability=availability), self.assertRaises(HTTPException) as error:
                put_supplier_config('synthetic', {'adapter_settings': {'availability': availability}})
            self.assertEqual(error.exception.status_code, 422)
            self.assertEqual(error.exception.detail['code'], 'supplier_availability_invalid')
            self.assertFalse(config_io.supplier_path('synthetic').exists())
