"""Shopee adapter implementing the common publisher interface."""

from __future__ import annotations

from typing import Any

from shopee_publisher import ShopeeAPIClient, build_shopee_item_payload

from .base import PlatformPublishResult, PlatformPublisher


class ShopeePublisher(PlatformPublisher):
    platform = "shopee"

    def __init__(self, *, client: ShopeeAPIClient | None = None) -> None:
        self.client = client or ShopeeAPIClient.from_env()

    def build_payload(self, listing_data: dict[str, Any]) -> dict[str, Any]:
        return build_shopee_item_payload(listing_data)

    def publish(self, listing_data: dict[str, Any]) -> PlatformPublishResult:
        response = self.client.upload_as_draft(listing_data)
        item_id = response.get("item_id") or response.get("item_id_list")
        return PlatformPublishResult(
            platform=self.platform,
            success=True,
            status="UNLIST",
            external_id=str(item_id) if item_id is not None else None,
            raw_response=response,
        )