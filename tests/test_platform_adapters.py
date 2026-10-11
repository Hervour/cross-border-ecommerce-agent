from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from typing import Any

from ecommerce_agent.models import ProductVariation, StandardProductListing
from platform_adapters.aliexpress import (
    AliExpressPublisher,
    build_aliexpress_product_payload,
    generate_aliexpress_signature,
)
from platform_adapters.base import PlatformValidationError
from platform_adapters.order_adapters import AliExpressOrderAdapter, TikTokShopOrderAdapter
from order_fulfillment import OrderFulfillmentService, create_app
from fastapi.testclient import TestClient
from platform_adapters.tiktok_shop import (
    TikTokShopPublisher,
    build_tiktok_shop_product_payload,
    generate_tiktok_shop_signature,
)


class _FakeAliExpressClient:
    def __init__(self) -> None:
        self.method = ""
        self.payload: dict[str, Any] = {}

    def call(self, method: str, business_params: dict[str, Any]) -> dict[str, Any]:
        self.method = method
        self.payload = business_params
        return {"aliexpress_ds_product_create_response": {"product_id": 98765}}


class _FakeTikTokClient:
    def __init__(self) -> None:
        self.path = ""
        self.payload: dict[str, Any] = {}

    def call(self, method: str, path: str, *, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self.path = path
        self.payload = body or {}
        return {"code": 0, "data": {"product_id": "TIKTOK-98765"}}


def _listing_data() -> dict[str, Any]:
    listing = StandardProductListing(
        title=(
            "Portable Folding Camping Chair Lightweight Aluminum Moon Chair "
            "for Outdoor Travel Beach Picnic"
        ),
        bullet_points=[
            "🪑 Compact folding frame fits easily in a car trunk",
            "🪶 Lightweight aluminum alloy frame for easy carrying",
            "🧵 Durable 600D Oxford fabric seat is easy to clean",
            "🎒 Side mesh pocket keeps a bottle and phone within reach",
            "🏕️ Suitable for camping, fishing, picnics, and outdoor concerts",
        ],
        description="## Portable Chair\nA lightweight folding chair for outdoor seating.",
        category="Sports & Outdoors > Camping Chairs",
        tags=["camping chair", "folding chair", "outdoor chair", "moon chair", "beach chair"],
        variations=[
            ProductVariation(
                original_spec="米白色",
                en_spec="Beige",
                cost_cny=58.0,
                target_price_sgd=23.50,
            ),
            ProductVariation(
                original_spec="军绿色",
                en_spec="Army Green",
                cost_cny=69.0,
                target_price_sgd=27.30,
            ),
        ],
    )
    data = listing.model_dump()
    data.update(
        {
            "category_id": 100017,
            "image_urls": ["https://example.com/chair.jpg"],
            "tiktok_image_uri_list": ["tos-image-uri-1"],
            "currency_code": "USD",
            "sgd_to_target_currency_rate": 0.75,
            "weight": 1.2,
        }
    )
    return data

class SignatureTests(unittest.TestCase):
    def test_aliexpress_md5_signature(self) -> None:
        params = {"method": "aliexpress.ds.product.create", "app_key": "123", "v": "2.0"}
        signature = generate_aliexpress_signature("secret", params)
        plain = "secret" + "".join(f"{key}{params[key]}" for key in sorted(params)) + "secret"
        expected = hashlib.md5(plain.encode("utf-8")).hexdigest().upper()
        self.assertEqual(signature, expected)

    def test_tiktok_shop_hmac_signature(self) -> None:
        path = "/product/202309/products"
        params = {"app_key": "123", "timestamp": 1700000000, "shop_cipher": "cipher"}
        body = {"title": "Chair"}
        signature = generate_tiktok_shop_signature("secret", path, params, body)
        concatenated = "".join(f"{key}{params[key]}" for key in sorted(params))
        body_text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        plain = f"secret{path}{concatenated}{body_text}secret"
        expected = hmac.new(b"secret", plain.encode("utf-8"), hashlib.sha256).hexdigest()
        self.assertEqual(signature, expected)


class AliExpressAdapterTests(unittest.TestCase):
    def test_payload_maps_price_and_images(self) -> None:
        payload = build_aliexpress_product_payload(_listing_data())

        self.assertEqual(payload["product_title"], _listing_data()["title"])
        self.assertEqual(payload["category_id"], 100017)
        self.assertEqual(payload["images"][0]["image_url"], "https://example.com/chair.jpg")
        self.assertEqual(payload["sku_list"][0]["price"], 17.63)
        self.assertEqual(payload["product_status"], "draft")

    def test_publish_returns_platform_result(self) -> None:
        client = _FakeAliExpressClient()
        result = AliExpressPublisher(client=client).publish(_listing_data())

        self.assertTrue(result.success)
        self.assertEqual(result.external_id, "98765")
        self.assertEqual(client.method, "aliexpress.ds.product.create")

    def test_image_urls_are_required(self) -> None:
        data = _listing_data()
        data["image_urls"] = []
        with self.assertRaises(PlatformValidationError):
            build_aliexpress_product_payload(data)


class TikTokShopAdapterTests(unittest.TestCase):
    def test_payload_maps_draft_and_image_uris(self) -> None:
        payload = build_tiktok_shop_product_payload(_listing_data())

        self.assertEqual(payload["status"], "DRAFT")
        self.assertEqual(payload["main_images"], [{"uri": "tos-image-uri-1"}])
        self.assertEqual(payload["skus"][0]["price"]["amount"], "17.63")
        self.assertEqual(payload["package_weight"]["unit"], "KILOGRAM")

    def test_publish_returns_platform_result(self) -> None:
        client = _FakeTikTokClient()
        result = TikTokShopPublisher(client=client).publish(_listing_data())

        self.assertTrue(result.success)
        self.assertEqual(result.external_id, "TIKTOK-98765")
        self.assertEqual(client.path, "/product/202309/products")

    def test_image_uri_is_required(self) -> None:
        data = _listing_data()
        data["tiktok_image_uri_list"] = []
        with self.assertRaises(PlatformValidationError):
            build_tiktok_shop_product_payload(data)


class OrderAdapterTests(unittest.TestCase):
    def test_aliexpress_order_webhook_normalization(self) -> None:
        payload = {
            "data": {
                "order_id": "ALI-ORDER-1",
                "payment_amount": 80,
                "currency": "USD",
                "product_list": [{"sku_code": "SHOPEE-SKU-CAMP-BEIGE", "quantity": 1}],
                "receipt_address": {"name": "Buyer", "city": "New York", "country": "US"},
            }
        }
        normalized = AliExpressOrderAdapter().parse_webhook(payload)
        self.assertEqual(normalized["platform_order_id"], "ALI-ORDER-1")
        self.assertEqual(normalized["items"][0]["sku_id"], "SHOPEE-SKU-CAMP-BEIGE")
        self.assertEqual(normalized["delivery_address"]["city"], "New York")

    def test_tiktok_order_webhook_normalization(self) -> None:
        payload = {
            "data": {
                "order_id": "TT-ORDER-1",
                "payment": {"total_amount": "90", "currency": "USD"},
                "line_items": [{"seller_sku": "SHOPEE-SKU-CAMP-BEIGE", "quantity": 1}],
                "recipient_address": {"name": "Buyer", "city": "Los Angeles", "region_code": "US"},
            }
        }
        normalized = TikTokShopOrderAdapter().parse_webhook(payload)
        self.assertEqual(normalized["platform_order_id"], "TT-ORDER-1")
        self.assertEqual(normalized["paid_amount"], 90.0)
        self.assertEqual(normalized["delivery_address"]["city"], "Los Angeles")

    def test_aliexpress_webhook_route_uses_platform_parser(self) -> None:
        service = OrderFulfillmentService(notifier=lambda details: {"sent": True})
        client = TestClient(create_app(service))
        payload = {
            "data": {
                "order_id": "ALI-ORDER-2",
                "paid_amount": 100,
                "currency": "CNY",
                "items": [{"sku_id": "SHOPEE-SKU-CAMP-BEIGE", "quantity": 1}],
                "shipping_address": {"name": "Buyer", "city": "Paris", "country": "FR"},
            }
        }

        response = client.post("/api/v1/aliexpress/order_webhook", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["platform"], "aliexpress")


if __name__ == "__main__":
    unittest.main()