import unittest

from inventory_hub.services.atomer_source_state import native_catalog_code, parse_admin


class RecoveredIdentityTests(unittest.TestCase):
    def test_only_lossless_whole_sku_with_independent_evidence_is_recovered(self):
        actual='BR-200-RED'
        raw=actual.encode('utf-16le').decode('ascii')
        for value in [raw,raw[:-1]]:
            self.assertEqual(native_catalog_code(value,'BR-200x','4006381333931'),actual)
            self.assertEqual(native_catalog_code(value,'BR-300x','4006381333931'),value)
            self.assertEqual(native_catalog_code(value,'BR-200x','12345'),value)
        self.assertEqual(native_catalog_code('BR-200-RE\x00D','BR-200x','4006381333931'),'BR-200-RE\x00D')

    def test_duplicate_card_keeps_both_references_and_checks_commercial_content(self):
        header='Interné číslo;Kat. číslo rodiča;Kat. číslo;EAN;Viditeľný;Bežná cena;Obrázky\n'
        item=';P;SKU;4006381333931;1;19,5;'
        rows='21'+item+'first.jpg\n22'+item+'second.jpg\n'
        agreed=parse_admin((header+rows).encode())['SKU']
        self.assertEqual(agreed['internal_ids'],['21','22'])
        self.assertFalse(agreed.get('conflict'))
        self.assertTrue(parse_admin((header+rows.replace('19,5;second','25;second')).encode())['SKU']['conflict'])
