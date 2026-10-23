"""Only explicit, matching shop observations supply currency and VAT context."""
import unittest
from inventory_hub.services.product_trace import shop_price_context


class TracePriceTests(unittest.TestCase):
    def test_language_currency_belongs_to_the_selected_price(self):
        prices = [{'language': 'sk', 'pricelists': [{'price_with_vat': '12.34'}]},
                  {'language': 'cs', 'pricelists': [{'price_with_vat': '300'}]}]
        options = {'languages': [{'code': 'sk', 'currency': 'EUR'}, {'code': 'cs', 'currency': 'CZK'}]}
        self.assertEqual(shop_price_context(prices, '12.34', options), ('EUR', True))
        self.assertEqual(shop_price_context(prices, '300', options), (None, None))
        self.assertEqual(shop_price_context(prices, '12.34', {}), (None, True))

    def test_explicit_zero_price_currency_and_unknown_tax_are_preserved(self):
        self.assertEqual(shop_price_context({'currency': 'czk', 'price': '0'}, '0', {}), ('CZK', None))
        self.assertEqual(shop_price_context(None, '0', {}), (None, None))
        self.assertEqual(shop_price_context({'price_with_vat': 'NaN'}, '0', {}), (None, None))
        self.assertEqual(shop_price_context({'price_with_vat': '1'}, None, {}), (None, None))
