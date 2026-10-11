from __future__ import annotations

import unittest

from category_mapper import CategoryMapper


class CategoryMapperTests(unittest.TestCase):
    def test_resolves_aliexpress_category(self) -> None:
        mapper = CategoryMapper()
        result = mapper.resolve("aliexpress", "Sports & Outdoors > Camping Chairs")

        self.assertIsNotNone(result)
        self.assertEqual(result["category_id"], 100017)

    def test_enriches_tiktok_attributes(self) -> None:
        mapper = CategoryMapper()
        result = mapper.enrich(
            "tiktok_shop",
            {"category": "Camping Chair", "title": "Portable moon chair"},
        )

        self.assertEqual(result["category_id"], "601234")
        self.assertEqual(result["product_attributes"][0]["name"], "Material")


if __name__ == "__main__":
    unittest.main()