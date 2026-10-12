"""Price configuration checks use temporary files and synthetic suppliers only."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from inventory_hub import config_io
from inventory_hub.config_normalize import normalize_supplier_config, normalize_supplier_price_coefficients
from inventory_hub.routers import suppliers


class SupplierPriceCoefficientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for target in (config_io, suppliers):
            patcher = patch.object(target, 'DATA_ROOT', root)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_brand_coefficients_preserve_decimal_strings_and_unrelated_configuration(self):
        coefficients = {f'Brand {index}': 1 for index in range(37)}
        coefficients['Brand & Wheel'] = '0.95000000000000000001'
        config = {'name': 'Synthetic supplier', 'adapter_settings': {
            'price_coefficients': coefficients, 'custom': {'retained': True},
        }, 'feeds': {'sources': {'products': {'remote': {'url': '', 'urls': ['https://supplier.example.test/a.xml']}}}}}
        before = copy.deepcopy(config)
        result = suppliers.put_supplier_config('synthetic', config)
        loaded = config_io.load_supplier('synthetic', write_back_on_load=False)
        self.assertEqual(config, before)
        self.assertEqual(result['adapter_settings']['price_coefficients'], coefficients)
        self.assertEqual(loaded['adapter_settings']['price_coefficients'], coefficients)
        self.assertEqual(loaded['adapter_settings']['custom'], {'retained': True})
        self.assertEqual(loaded['feeds'], config['feeds'] | {'current_key': 'products'})

    def test_invalid_coefficients_are_rejected_before_history_or_config_writes(self):
        invalid = ([], {'': 1}, {'Test': 1, 'test': 2}, {'Test': True}, {'Test': None},
                   {'Test': 0}, {'Test': -1}, {'Test': ''}, {'Test': 'NaN'},
                   {'Test': 'Infinity'}, {'Test': 'not-a-number'}, {'Test': float('inf')})
        for coefficients in invalid:
            with self.subTest(coefficients=coefficients), patch.object(suppliers, '_save_to_history') as history:
                with self.assertRaises(HTTPException) as error:
                    suppliers.put_supplier_config('synthetic', {'adapter_settings': {'price_coefficients': coefficients}})
                self.assertEqual(error.exception.status_code, 422)
                self.assertEqual(error.exception.detail['code'], 'invalid_price_coefficient')
                history.assert_not_called()
                self.assertFalse(config_io.supplier_path('synthetic').exists())

    def test_legacy_number_values_and_missing_coefficients_remain_compatible(self):
        coefficients = {'Shimano': 0.9, 'SCHWALBE': 1, 'Other': '1.050'}
        config = normalize_supplier_config({'price_coefficients': coefficients})
        self.assertEqual(config['adapter_settings']['price_coefficients'], coefficients)
        self.assertEqual(normalize_supplier_price_coefficients(), {})
        self.assertEqual(normalize_supplier_config({})['adapter_settings']['price_coefficients'], {})
        self.assertTrue(suppliers._validate_config_syntax({'adapter_settings': {'price_coefficients': coefficients}}).valid)
        self.assertFalse(suppliers._validate_config_syntax({'adapter_settings': {'price_coefficients': {'Test': 'NaN'}}}).valid)

    def test_history_restore_cannot_reintroduce_an_invalid_coefficient(self):
        with patch.object(suppliers, '_load_history_version', return_value={'adapter_settings': {'price_coefficients': {'Test': -1}}}):
            with self.assertRaises(HTTPException) as error:
                suppliers.restore_supplier_version('synthetic', 'older')
        self.assertEqual(error.exception.detail['code'], 'invalid_price_coefficient')
        self.assertFalse(config_io.supplier_path('synthetic').exists())
