from __future__ import annotations

import unittest
from unittest.mock import patch

import requests

from source_scraper import (
    RawProductData,
    fetch_product_by_url,
    load_mock_html,
    load_mock_json_text,
    parse_1688_html,
)


class _FailingSession:
    def get(self, *args: object, **kwargs: object) -> object:
        raise requests.ConnectionError("blocked by test")

    def close(self) -> None:
        pass


class SourceScraperTests(unittest.TestCase):
    def test_parse_mock_html_extracts_core_fields(self) -> None:
        product = parse_1688_html(load_mock_html())

        self.assertIsInstance(product, RawProductData)
        self.assertEqual(product.source_url, "mock://1688/camping-chair")
        self.assertIn("折叠露营椅", product.original_title)
        self.assertEqual(product.price_range_cny, "58.00-69.00")
        self.assertEqual(product.attributes["框架材质"], "铝合金")
        self.assertEqual(len(product.image_urls), 3)
        self.assertEqual(len(product.skus), 3)
        self.assertEqual(product.skus[2]["cost_cny"], 69.0)
        self.assertTrue(product.skus[0]["image_url"].startswith("https://"))

    def test_parse_mock_json_maps_standard_fields(self) -> None:
        product = parse_1688_html(load_mock_json_text())

        self.assertEqual(product.source_url, "mock://1688/camping-chair-json")
        self.assertEqual(len(product.attributes), 6)
        self.assertEqual(len(product.image_urls), 3)
        self.assertEqual(len(product.skus), 3)
        self.assertEqual(product.skus[1]["original_spec"], "黑色 标准款 铝合金支架")

    def test_test_text_loads_local_mock(self) -> None:
        product = parse_1688_html("__MOCK_1688__")

        self.assertIn("折叠露营椅", product.original_title)
        self.assertEqual(product.price_range_cny, "58.00-69.00")

    def test_fetch_mock_url(self) -> None:
        html = fetch_product_by_url("mock://1688/camping-chair")

        self.assertIn("mock-sku-data", html)
        self.assertIn("折叠露营椅", html)

    def test_network_failure_falls_back_to_mock(self) -> None:
        with patch("source_scraper._build_session", return_value=_FailingSession()):
            html = fetch_product_by_url("https://detail.1688.com/offer/123456.html")

        product = parse_1688_html(html)
        self.assertEqual(product.price_range_cny, "58.00-69.00")
        self.assertEqual(len(product.skus), 3)


if __name__ == "__main__":
    unittest.main()