"""Remote parent codes identify content only within their own shop."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from inventory_hub.routers.stock import _image_urls_by_product


class StockShopContentTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_parent_code_in_two_shops_keeps_its_own_image(self):
        images = [SimpleNamespace(shop_id=1, external_code="PARENT", url="https://example.com/a.jpg"),
                  SimpleNamespace(shop_id=2, external_code="PARENT", url="https://example.com/b.jpg")]
        mappings = Mock()
        mappings.all.return_value = [(11, 1, "ITEM-A", "PARENT"), (22, 2, "ITEM-B", "PARENT"),
                                     (33, 3, "ITEM-C", "PARENT")]
        db = SimpleNamespace(execute=AsyncMock(side_effect=[images, mappings]))
        self.assertEqual(await _image_urls_by_product(db), {
            11: "https://example.com/a.jpg", 22: "https://example.com/b.jpg",
        })
