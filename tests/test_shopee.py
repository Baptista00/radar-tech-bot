import unittest
from unittest.mock import Mock

from services.shopee import ShopeeConfig, ShopeeSource


class ShopeeCandidateTests(unittest.TestCase):
    def test_price_max_is_variation_price_not_original_price(self):
        client = Mock(generate_short_link=Mock(return_value="https://s.shopee.com.br/x"))
        source = ShopeeSource(ShopeeConfig(app_id="id", secret="secret"), client=client)
        node = {"itemId": 123, "productName": "Mouse gamer", "priceMin": "79.90", "priceMax": "129.90",
                "offerLink": "https://shopee.com.br/p", "sales": 500, "ratingStar": "4.8"}
        candidate = source._candidate(node, "mouse gamer")
        self.assertEqual(candidate.price, 7990)
        self.assertIsNone(candidate.original_price)


if __name__ == "__main__":
    unittest.main()
