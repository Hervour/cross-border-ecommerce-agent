"""Platform order webhook and tracking adapters."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any, Mapping

from .aliexpress import AliExpressAPIClient
from .tiktok_shop import TikTokShopAPIClient


def _first(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", []):
            return value
    return None


def _as_float(value: Any) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


class PlatformOrderAdapter(ABC):
    platform: str

    @abstractmethod
    def parse_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Normalize a platform order payload for OrderFulfillmentService."""

    @abstractmethod
    def sync_tracking(
        self, order_id: str, tracking_code: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Send tracking information back to the marketplace."""


class AliExpressOrderAdapter(PlatformOrderAdapter):
    platform = "aliexpress"

    def __init__(self, *, client: AliExpressAPIClient | None = None) -> None:
        self.client = client

    def parse_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("data", payload)
        if not isinstance(data, Mapping):
            raise ValueError("AliExpress order payload must be an object")
        raw_items = _first(data, ("product_list", "items", "order_items", "products")) or []
        if isinstance(raw_items, Mapping):
            raw_items = [raw_items]
        items: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, Mapping):
                continue
            sku_id = _first(item, ("sku_id", "sku_code", "seller_sku", "product_id"))
            if sku_id in (None, ""):
                continue
            items.append(
                {
                    "sku_id": str(sku_id),
                    "quantity": max(1, int(_as_float(item.get("quantity", item.get("num", 1))))),
                    "product_name": str(item.get("product_name") or item.get("subject") or ""),
                    "paid_amount": _as_float(item.get("price") or item.get("amount")),
                }
            )
        address = _first(data, ("receipt_address", "shipping_address", "logistics_address")) or {}
        if not isinstance(address, Mapping):
            address = {}
        return {
            "platform": self.platform,
            "platform_order_id": str(_first(data, ("order_id", "trade_order_id", "order_sn")) or ""),
            "shop_id": _first(data, ("shop_id", "seller_id")),
            "status": str(data.get("order_status") or data.get("status") or ""),
            "currency": str(data.get("currency") or data.get("currency_code") or "USD").upper(),
            "paid_amount": _as_float(
                _first(data, ("payment_amount", "paid_amount", "order_amount", "total_amount"))
            ),
            "items": items,
            "delivery_address": {
                "recipient_name": address.get("contact_person") or address.get("name") or "",
                "phone": address.get("phone_number") or address.get("phone") or "",
                "country": address.get("country") or "",
                "region": address.get("province") or address.get("state") or "",
                "city": address.get("city") or "",
                "address_line": address.get("address") or address.get("detail_address") or "",
                "postcode": address.get("zip") or address.get("postcode") or "",
                "address_type": address.get("address_type") or "dropshipping",
                "full_address": address.get("full_address") or address.get("address") or "",
            },
        }

    def sync_tracking(
        self, order_id: str, tracking_code: str, **kwargs: Any
    ) -> dict[str, Any]:
        client = self.client or AliExpressAPIClient.from_env()
        method = os.environ.get(
            "ALIEXPRESS_TRACKING_METHOD", "aliexpress.logistics.ds.trackinginfo.update"
        )
        payload = {
            "order_id": order_id,
            "tracking_number": tracking_code,
            "logistics_no": tracking_code,
        }
        payload.update(kwargs)
        return client.call(method, payload)

class TikTokShopOrderAdapter(PlatformOrderAdapter):
    platform = "tiktok_shop"

    def __init__(self, *, client: TikTokShopAPIClient | None = None) -> None:
        self.client = client

    def parse_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("data", payload)
        if not isinstance(data, Mapping):
            raise ValueError("TikTok Shop order payload must be an object")
        raw_items = _first(data, ("line_items", "items", "order_items", "products")) or []
        if isinstance(raw_items, Mapping):
            raw_items = [raw_items]
        items: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, Mapping):
                continue
            sku_id = _first(item, ("seller_sku", "sku_id", "sku_code", "product_id"))
            if sku_id in (None, ""):
                continue
            items.append(
                {
                    "sku_id": str(sku_id),
                    "quantity": max(1, int(_as_float(item.get("quantity", 1)))),
                    "product_name": str(item.get("product_name") or item.get("name") or ""),
                    "paid_amount": _as_float(item.get("sale_price") or item.get("price")),
                }
            )
        payment = data.get("payment") if isinstance(data.get("payment"), Mapping) else {}
        address = _first(data, ("recipient_address", "shipping_address", "delivery_address")) or {}
        if not isinstance(address, Mapping):
            address = {}
        return {
            "platform": self.platform,
            "platform_order_id": str(_first(data, ("order_id", "id", "order_sn")) or ""),
            "shop_id": _first(data, ("shop_id", "seller_id")),
            "status": str(data.get("status") or data.get("order_status") or ""),
            "currency": str(data.get("currency") or payment.get("currency") or "USD").upper(),
            "paid_amount": _as_float(
                _first(data, ("paid_amount", "total_amount"))
                or _first(payment, ("total_amount", "paid_amount"))
            ),
            "items": items,
            "delivery_address": {
                "recipient_name": address.get("name") or address.get("recipient_name") or "",
                "phone": address.get("phone") or address.get("phone_number") or "",
                "country": address.get("region_code") or address.get("country") or "",
                "region": address.get("state") or address.get("province") or "",
                "city": address.get("city") or "",
                "address_line": address.get("address") or address.get("address_line1") or "",
                "postcode": address.get("postal_code") or address.get("zip") or "",
                "address_type": address.get("address_type") or "marketplace",
                "full_address": address.get("full_address") or address.get("address") or "",
            },
        }

    def sync_tracking(
        self, order_id: str, tracking_code: str, **kwargs: Any
    ) -> dict[str, Any]:
        client = self.client or TikTokShopAPIClient.from_env()
        path_template = os.environ.get(
            "TIKTOK_SHOP_TRACKING_PATH",
            "/fulfillment/202309/orders/{order_id}/packages",
        )
        path = path_template.format(order_id=order_id)
        body = {"tracking_number": tracking_code}
        body.update(kwargs)
        return client.call("POST", path, body=body)


ORDER_ADAPTERS: dict[str, type[PlatformOrderAdapter]] = {
    "aliexpress": AliExpressOrderAdapter,
    "tiktok_shop": TikTokShopOrderAdapter,
}


def get_order_adapter(platform: str, **kwargs: Any) -> PlatformOrderAdapter:
    key = platform.strip().lower().replace("-", "_")
    adapter_class = ORDER_ADAPTERS.get(key)
    if adapter_class is None:
        raise ValueError(
            f"Unsupported order platform '{platform}'. Available: {', '.join(sorted(ORDER_ADAPTERS))}"
        )
    return adapter_class(**kwargs)