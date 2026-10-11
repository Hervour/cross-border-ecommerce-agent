"""Marketplace publishing adapters."""

from .aliexpress import (
    AliExpressAPIClient,
    AliExpressPublisher,
    build_aliexpress_product_payload,
    generate_aliexpress_signature,
    publish_aliexpress_product,
)
from .base import (
    PlatformAPIError,
    PlatformAdapterError,
    PlatformPublisher,
    PlatformPublishResult,
    PlatformValidationError,
    round_money,
)
from .order_adapters import (
    ORDER_ADAPTERS,
    AliExpressOrderAdapter,
    PlatformOrderAdapter,
    TikTokShopOrderAdapter,
    get_order_adapter,
)
from .registry import PUBLISHERS, get_publisher
from .shopee import ShopeePublisher
from .tiktok_shop import (
    TikTokShopAPIClient,
    TikTokShopPublisher,
    build_tiktok_shop_product_payload,
    generate_tiktok_shop_signature,
    publish_tiktok_shop_product,
)

__all__ = [
    "AliExpressAPIClient",
    "AliExpressPublisher",
    "PUBLISHERS",
    "PlatformAPIError",
    "PlatformAdapterError",
    "PlatformPublisher",
    "PlatformPublishResult",
    "PlatformValidationError",
    "AliExpressOrderAdapter",
    "ORDER_ADAPTERS",
    "PlatformOrderAdapter",
    "ShopeePublisher",
    "TikTokShopOrderAdapter",
    "TikTokShopAPIClient",
    "TikTokShopPublisher",
    "build_aliexpress_product_payload",
    "build_tiktok_shop_product_payload",
    "generate_aliexpress_signature",
    "generate_tiktok_shop_signature",
    "get_order_adapter",
    "get_publisher",
    "publish_aliexpress_product",
    "publish_tiktok_shop_product",
    "round_money",
]