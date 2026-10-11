"""Platform publisher registry."""

from __future__ import annotations

from .aliexpress import AliExpressPublisher
from .base import PlatformPublisher, PlatformValidationError
from .shopee import ShopeePublisher
from .tiktok_shop import TikTokShopPublisher

PUBLISHERS: dict[str, type[PlatformPublisher]] = {
    "shopee": ShopeePublisher,
    "aliexpress": AliExpressPublisher,
    "tiktok_shop": TikTokShopPublisher,
}


def get_publisher(platform: str, **kwargs: object) -> PlatformPublisher:
    """Create a configured publisher for a platform name."""

    key = platform.strip().lower().replace("-", "_")
    publisher_class = PUBLISHERS.get(key)
    if publisher_class is None:
        raise PlatformValidationError(
            f"Unsupported platform '{platform}'. Available: {', '.join(sorted(PUBLISHERS))}"
        )
    return publisher_class(**kwargs)