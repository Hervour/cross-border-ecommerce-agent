"""Module 4: Shopee order webhook, sourcing decision, alerts, and tracking sync."""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode

import requests
import uvicorn
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from shopee_publisher import ShopeeAPIClient

LOGGER = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_MAPPING_PATH = BASE_DIR / "data" / "sku_mapping.json"
SHIP_ORDER_PATH = "/api/v2/logistics/ship_order"


class OrderFulfillmentError(Exception):
    """Base exception for order fulfillment failures."""


class OrderParseError(OrderFulfillmentError):
    """Raised when a Shopee webhook payload lacks required order information."""


class MappingNotFoundError(OrderFulfillmentError):
    """Raised when a Shopee SKU has no local supplier mapping."""


class DeliveryAddress(BaseModel):
    model_config = ConfigDict(extra="allow")

    recipient_name: str = ""
    phone: str = ""
    country: str = ""
    region: str = ""
    city: str = ""
    address_line: str = ""
    postcode: str = ""
    address_type: str = ""
    full_address: str = ""


class OrderItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    sku_id: str = Field(min_length=1)
    quantity: int = Field(default=1, ge=1)
    product_name: str = ""
    paid_amount: float | None = None


class NormalizedOrder(BaseModel):
    model_config = ConfigDict(extra="allow")

    platform: str = "shopee"
    shopee_order_id: str = Field(min_length=1)
    shop_id: int | None = None
    status: str = ""
    currency: str = "CNY"
    paid_amount: float = Field(default=0, ge=0)
    items: list[OrderItem] = Field(min_length=1)
    delivery_address: DeliveryAddress = Field(default_factory=DeliveryAddress)


class SkuMappingRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    shopee_sku: str = ""
    supplier_name: str = ""
    supplier_url: str = Field(min_length=1)
    supplier_spec: str = ""
    supplier_sku_id: str = ""
    purchase_cost_cny: float = Field(ge=0)
    shipping_cny: float = Field(default=0, ge=0)
    notes: str = ""


class SkuMappingStore:
    """Load Shopee SKU to 1688 sourcing mappings from a JSON file."""

    def __init__(self, mapping_path: str | Path = DEFAULT_MAPPING_PATH) -> None:
        self.mapping_path = Path(mapping_path)
        self.records = self._load()

    def _load(self) -> dict[str, SkuMappingRecord]:
        if not self.mapping_path.exists():
            raise MappingNotFoundError(f"SKU mapping file not found: {self.mapping_path}")
        payload = json.loads(self.mapping_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("mappings"), dict):
            payload = payload["mappings"]
        if not isinstance(payload, dict):
            raise MappingNotFoundError("SKU mapping JSON must contain an object")

        records: dict[str, SkuMappingRecord] = {}
        for sku, value in payload.items():
            if not isinstance(value, dict):
                continue
            try:
                records[str(sku)] = SkuMappingRecord.model_validate(value)
            except ValidationError as exc:
                raise MappingNotFoundError(f"Invalid mapping for SKU {sku}: {exc}") from exc
        return records

    def get(self, sku_id: str) -> SkuMappingRecord:
        key = str(sku_id).strip()
        if key in self.records:
            return self.records[key]
        for mapped_sku, record in self.records.items():
            if mapped_sku.casefold() == key.casefold():
                return record
        raise MappingNotFoundError(f"No 1688 mapping found for Shopee SKU: {sku_id}")

def _first_value(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", []):
            return value
    return None


def _to_float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _unwrap_order_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data: Any = payload.get("data", payload)
    if isinstance(data, Mapping) and isinstance(data.get("order"), Mapping):
        return data["order"]
    if isinstance(data, Mapping):
        return data
    raise OrderParseError("Shopee webhook data must be a JSON object")


def _extract_items(data: Mapping[str, Any]) -> list[OrderItem]:
    raw_items = _first_value(data, ("items", "item_list", "order_items", "products"))
    if isinstance(raw_items, Mapping):
        raw_items = [raw_items]
    if raw_items is None and _first_value(data, ("sku_id", "sku", "model_sku", "item_sku")):
        raw_items = [data]
    if not isinstance(raw_items, list):
        raise OrderParseError(
            "Webhook payload contains no order items. Enrich it with item/SKU data "
            "from v2.order.get_order_detail before calling the fulfillment service."
        )

    items: list[OrderItem] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            continue
        sku_id = _first_value(
            raw_item,
            ("sku_id", "model_sku", "item_sku", "sku", "seller_sku", "shopee_sku"),
        )
        if sku_id in (None, ""):
            continue
        items.append(
            OrderItem(
                sku_id=str(sku_id),
                quantity=max(1, int(_to_float(raw_item.get("quantity", raw_item.get("qty", 1)), 1))),
                product_name=str(
                    _first_value(raw_item, ("product_name", "item_name", "name")) or ""
                ),
                paid_amount=_first_value(
                    raw_item, ("paid_amount", "model_discounted_price", "price")
                ),
            )
        )
    if not items:
        raise OrderParseError("No valid SKU was found in the Shopee order payload")
    return items


def _extract_delivery_address(data: Mapping[str, Any]) -> DeliveryAddress:
    raw_address = _first_value(
        data,
        ("recipient_address", "shipping_address", "buyer_address", "delivery_address", "address"),
    )
    if not isinstance(raw_address, Mapping):
        raw_address = data

    parts = [
        _first_value(raw_address, ("town", "district")),
        _first_value(raw_address, ("city", "municipality")),
        _first_value(raw_address, ("state", "region", "province")),
        _first_value(raw_address, ("country",)),
        _first_value(raw_address, ("zipcode", "postcode", "postal_code")),
    ]
    joined = " ".join(str(part) for part in parts if part)
    return DeliveryAddress(
        recipient_name=str(_first_value(raw_address, ("name", "recipient_name", "buyer_name")) or ""),
        phone=str(_first_value(raw_address, ("phone", "phone_number", "recipient_phone")) or ""),
        country=str(_first_value(raw_address, ("country",)) or ""),
        region=str(_first_value(raw_address, ("state", "region", "province")) or ""),
        city=str(_first_value(raw_address, ("city", "municipality")) or ""),
        address_line=str(_first_value(raw_address, ("address", "address1", "detail", "full_address")) or ""),
        postcode=str(_first_value(raw_address, ("zipcode", "postcode", "postal_code")) or ""),
        address_type=str(
            _first_value(raw_address, ("address_type", "warehouse_type", "delivery_type"))
            or _first_value(data, ("address_type", "warehouse_type"))
            or ""
        ),
        full_address=str(
            _first_value(raw_address, ("full_address", "formatted_address")) or joined
        ),
    )


def parse_shopee_order_payload(payload: dict[str, Any]) -> NormalizedOrder:
    """Normalize common Shopee order webhook and enriched payload shapes."""

    if not isinstance(payload, dict):
        raise OrderParseError("Shopee webhook payload must be a JSON object")
    data = _unwrap_order_payload(payload)

    order_id = _first_value(data, ("ordersn", "order_sn", "order_id", "orderId", "order_number"))
    if order_id in (None, ""):
        raise OrderParseError("Shopee order ID is missing")

    return NormalizedOrder(
        shopee_order_id=str(order_id),
        shop_id=_to_int(_first_value(data, ("shop_id", "shopId")) or payload.get("shop_id")),
        status=str(_first_value(data, ("order_status", "status", "order_state")) or ""),
        currency=str(_first_value(data, ("currency", "currency_code")) or "CNY").upper(),
        paid_amount=_to_float(
            _first_value(
                data,
                (
                    "paid_amount",
                    "buyer_paid_amount",
                    "total_amount",
                    "order_total",
                    "actual_amount",
                    "amount",
                ),
            )
        ),
        items=_extract_items(data),
        delivery_address=_extract_delivery_address(data),
    )

def _confirmation_url(order_id: str, base_url: str, secret: str) -> str:
    if not base_url:
        return ""
    token = ""
    if secret:
        token = hmac.new(
            secret.encode("utf-8"), order_id.encode("utf-8"), hashlib.sha256
        ).hexdigest()[:24]
    query = {"order_id": order_id}
    if token:
        query["token"] = token
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}{urlencode(query)}"


def _format_alert_markdown(order_details: dict[str, Any]) -> str:
    order_id = order_details.get("shopee_order_id", "")
    platform_name = str(order_details.get("platform") or "shopee").upper()
    profit = float(order_details.get("estimated_profit_cny", 0))
    status = order_details.get("status", "")
    supplier_links = order_details.get("supplier_links") or []
    address = order_details.get("delivery_address") or {}
    confirm_url = order_details.get("confirmation_url") or ""

    lines = [
        f"## 新订单代采提醒 · {platform_name} {status}",
        f"**{platform_name} 订单号:** {order_id}",
        f"**预计利润:** CNY {profit:.2f}",
        "",
        "### 1688 采购清单",
    ]
    for item in supplier_links:
        lines.append(
            f"- [{item.get('supplier_spec') or item.get('sku_id')}]"
            f"({item.get('supplier_url')}) · CNY {float(item.get('total_cost_cny', 0)):.2f}"
        )
    lines.extend(
        [
            "",
            "### 买家送货信息",
            f"- 收件人: {address.get('recipient_name', '')}",
            f"- 电话: {address.get('phone', '')}",
            f"- 地址: {address.get('full_address', '')}",
            f"- 类型: {address.get('address_type', '')}",
        ]
    )
    if confirm_url:
        lines.extend(["", f"[点击确认代采]({confirm_url})"])
    return "\n".join(lines)


class PurchaseNotifier:
    """Send purchase alerts to WeCom, Feishu, or Telegram."""

    def __init__(
        self,
        *,
        wecom_webhook_url: str = "",
        feishu_webhook_url: str = "",
        telegram_bot_token: str = "",
        telegram_chat_id: str = "",
        confirmation_base_url: str = "",
        confirmation_secret: str = "",
        session: requests.Session | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.wecom_webhook_url = wecom_webhook_url
        self.feishu_webhook_url = feishu_webhook_url
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self.confirmation_base_url = confirmation_base_url
        self.confirmation_secret = confirmation_secret
        self.session = session or requests.Session()
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "PurchaseNotifier":
        return cls(
            wecom_webhook_url=os.environ.get("WECOM_WEBHOOK_URL", ""),
            feishu_webhook_url=os.environ.get("FEISHU_WEBHOOK_URL", ""),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
            confirmation_base_url=os.environ.get("PURCHASE_CONFIRM_BASE_URL", ""),
            confirmation_secret=os.environ.get("PURCHASE_CONFIRM_SECRET", ""),
            timeout=float(os.environ.get("NOTIFICATION_TIMEOUT", "15")),
        )

    def send(self, order_details: dict[str, Any]) -> dict[str, Any]:
        order_id = str(order_details.get("shopee_order_id", ""))
        confirm_url = _confirmation_url(
            order_id, self.confirmation_base_url, self.confirmation_secret
        )
        details = dict(order_details)
        details["confirmation_url"] = confirm_url
        markdown = _format_alert_markdown(details)

        results: dict[str, Any] = {}
        if self.wecom_webhook_url:
            results["wecom"] = self._post(
                "wecom",
                self.wecom_webhook_url,
                {"msgtype": "markdown", "markdown": {"content": markdown}},
            )
        if self.feishu_webhook_url:
            results["feishu"] = self._post(
                "feishu",
                self.feishu_webhook_url,
                self._feishu_payload(details, markdown, confirm_url),
            )
        if self.telegram_bot_token and self.telegram_chat_id:
            telegram_url = (
                f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
            )
            results["telegram"] = self._post(
                "telegram",
                telegram_url,
                self._telegram_payload(details, markdown, confirm_url),
            )

        if not results:
            LOGGER.warning(
                "No notification channel configured; purchase alert was not sent for order %s",
                order_id,
            )
            return {"sent": False, "channels": {}, "message": markdown}

        return {
            "sent": any(item.get("ok") for item in results.values()),
            "channels": results,
            "message": markdown,
        }

    def _post(self, channel: str, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.session.post(url, json=payload, timeout=self.timeout)
            response.raise_for_status()
            data: Any
            try:
                data = response.json()
            except ValueError:
                data = response.text
            return {"ok": True, "response": data}
        except requests.RequestException as exc:
            LOGGER.error("%s purchase alert failed: %s", channel, exc)
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _feishu_payload(
        order_details: dict[str, Any], markdown: str, confirm_url: str
    ) -> dict[str, Any]:
        actions: list[dict[str, Any]] = []
        if confirm_url:
            actions.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "确认代采"},
                    "type": "primary",
                    "url": confirm_url,
                }
            )
        elements: list[dict[str, Any]] = [
            {"tag": "div", "text": {"tag": "lark_md", "content": markdown}}
        ]
        if actions:
            elements.append({"tag": "action", "actions": actions})
        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"Shopee 代采提醒 {order_details.get('shopee_order_id', '')}",
                    }
                },
                "elements": elements,
            },
        }

    def _telegram_payload(
        self, order_details: dict[str, Any], markdown: str, confirm_url: str
    ) -> dict[str, Any]:
        from html import escape

        plain = markdown.replace("**", "").replace("## ", "")
        text = escape(plain)
        payload: dict[str, Any] = {
            "chat_id": self.telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if confirm_url:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": "确认代采", "url": confirm_url}]]
            }
        return payload


def send_purchase_alert(order_details: dict[str, Any]) -> dict[str, Any]:
    """Send a human-in-the-loop procurement alert using configured channels."""

    return PurchaseNotifier.from_env().send(order_details)

class OrderFulfillmentService:
    """Match Shopee SKUs to 1688 sources and decide procurement."""

    def __init__(
        self,
        *,
        mapping_path: str | Path = DEFAULT_MAPPING_PATH,
        notifier: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        paid_to_cny_rate: float = 1.0,
        store: Any | None = None,
    ) -> None:
        self.store = store
        if store is None:
            self.mapping_store = SkuMappingStore(mapping_path)
        else:
            from fulfillment_store import SQLiteMappingStore

            self.mapping_store = SQLiteMappingStore(
                store, import_json_path=mapping_path, platform="shopee"
            )
        self.notifier = notifier or send_purchase_alert
        self.paid_to_cny_rate = paid_to_cny_rate

    def process_webhook(
        self, payload: dict[str, Any], *, platform: str = "shopee"
    ) -> dict[str, Any]:
        normalized_platform = platform.strip().lower().replace("-", "_")
        if normalized_platform == "shopee":
            order = parse_shopee_order_payload(payload)
        else:
            from platform_adapters.order_adapters import get_order_adapter

            adapter = get_order_adapter(normalized_platform)
            normalized = adapter.parse_webhook(payload)
            normalized["shopee_order_id"] = normalized.pop("platform_order_id", "")
            order = NormalizedOrder.model_validate(normalized)

        if self.store is not None:
            payload_hash = hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            event_id = str(payload.get("event_id") or payload.get("eventId") or payload_hash)
            is_new = self.store.register_webhook_event(
                platform=order.platform,
                event_id=event_id,
                payload_hash=payload_hash,
                order_id=order.shopee_order_id,
                payload=payload,
            )
            if not is_new:
                existing = self.store.get_order(order.platform, order.shopee_order_id)
                if existing:
                    duplicate = dict(existing["decision"])
                    duplicate["duplicate"] = True
                    return duplicate
                return {
                    "platform": order.platform,
                    "shopee_order_id": order.shopee_order_id,
                    "status": "DUPLICATE",
                    "duplicate": True,
                }

        result = self.process_order(order)
        if self.store is not None:
            self.store.save_order(
                platform=order.platform,
                order_id=order.shopee_order_id,
                status=str(result.get("status") or ""),
                profit_cny=float(result.get("estimated_profit_cny") or 0),
                payload=payload,
                decision=result,
            )
        return result

    def confirm_purchase(self, platform: str, order_id: str) -> bool:
        if self.store is None:
            return False
        return self.store.confirm_order(platform, order_id)

    def process_order(self, order: NormalizedOrder) -> dict[str, Any]:
        supplier_links: list[dict[str, Any]] = []
        missing_mappings: list[str] = []
        total_purchase_cost_cny = 0.0

        for item in order.items:
            try:
                mapping = self.mapping_store.get(item.sku_id)
            except MappingNotFoundError as exc:
                missing_mappings.append(item.sku_id)
                supplier_links.append(
                    {
                        "sku_id": item.sku_id,
                        "supplier_url": "",
                        "supplier_spec": "映射缺失",
                        "quantity": item.quantity,
                        "unit_cost_cny": 0.0,
                        "shipping_cny": 0.0,
                        "total_cost_cny": 0.0,
                        "error": str(exc),
                    }
                )
                continue

            line_cost = (mapping.purchase_cost_cny + mapping.shipping_cny) * item.quantity
            total_purchase_cost_cny += line_cost
            supplier_links.append(
                {
                    "sku_id": item.sku_id,
                    "supplier_name": mapping.supplier_name,
                    "supplier_url": mapping.supplier_url,
                    "supplier_spec": mapping.supplier_spec,
                    "supplier_sku_id": mapping.supplier_sku_id,
                    "quantity": item.quantity,
                    "unit_cost_cny": mapping.purchase_cost_cny,
                    "shipping_cny": mapping.shipping_cny,
                    "total_cost_cny": round(line_cost, 2),
                    "notes": mapping.notes,
                }
            )

        paid_amount_cny = order.paid_amount * self.paid_to_cny_rate
        if order.currency not in {"CNY", "RMB"} and self.paid_to_cny_rate == 1.0:
            LOGGER.warning(
                "Order %s is in %s but paid_to_cny_rate=1.0; verify profit calculation",
                order.shopee_order_id,
                order.currency,
            )

        estimated_profit_cny = round(paid_amount_cny - total_purchase_cost_cny, 2)
        if missing_mappings:
            status = "ANOMALY_MAPPING_MISSING"
        elif estimated_profit_cny < 0:
            status = "ANOMALY_LOW_PROFIT"
        else:
            status = "READY_FOR_PURCHASE"

        order_details = {
            "platform": order.platform,
            "shopee_order_id": order.shopee_order_id,
            "shop_id": order.shop_id,
            "status": status,
            "currency": order.currency,
            "paid_amount": order.paid_amount,
            "paid_amount_cny": round(paid_amount_cny, 2),
            "total_purchase_cost_cny": round(total_purchase_cost_cny, 2),
            "estimated_profit_cny": estimated_profit_cny,
            "missing_mappings": missing_mappings,
            "supplier_links": supplier_links,
            "delivery_address": order.delivery_address.model_dump(),
            "is_anomaly": bool(missing_mappings) or estimated_profit_cny < 0,
        }

        alert_result = self.notifier(order_details)
        order_details["alert_result"] = alert_result
        return order_details

def verify_confirmation_token(order_id: str, token: str, secret: str) -> bool:
    if not secret:
        return True
    expected = hmac.new(
        secret.encode("utf-8"), order_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:24]
    return hmac.compare_digest(expected, token)


def sync_tracking_number(
    shopee_order_id: str,
    tracking_code: str,
    *,
    client: ShopeeAPIClient | None = None,
    logistics_id: int | None = None,
    package_number: str | None = None,
) -> dict[str, Any]:
    """Sync a supplier tracking number to Shopee and mark the order shipped."""

    order_id = str(shopee_order_id).strip()
    tracking = str(tracking_code).strip()
    if not order_id:
        raise OrderFulfillmentError("shopee_order_id cannot be empty")
    if not tracking:
        raise OrderFulfillmentError("tracking_code cannot be empty")

    api_client = client or ShopeeAPIClient.from_env()
    payload: dict[str, Any] = {
        "order_sn": order_id,
        "non_integrated": {"tracking_number": tracking},
    }
    if logistics_id is not None:
        payload["logistics_id"] = logistics_id
    if package_number:
        payload["package_number"] = package_number

    result = api_client.call_api(SHIP_ORDER_PATH, payload)
    LOGGER.info("Tracking %s synced to Shopee order %s", tracking, order_id)
    return result


def create_app(service: OrderFulfillmentService | None = None) -> FastAPI:
    if service is None:
        from fulfillment_store import FulfillmentStore

        db_path = os.environ.get(
            "ORDER_DB_PATH", str(BASE_DIR / "data" / "fulfillment.db")
        )
        store = FulfillmentStore(db_path)
        fulfillment_service = OrderFulfillmentService(
            mapping_path=os.environ.get("ORDER_MAPPING_PATH", str(DEFAULT_MAPPING_PATH)),
            paid_to_cny_rate=float(os.environ.get("ORDER_PAID_TO_CNY_RATE", "1")),
            store=store,
        )
    else:
        fulfillment_service = service
    app = FastAPI(title="Cross-border order fulfillment service")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return """
        <!doctype html>
        <html lang="zh-CN">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>跨境代发订单服务</title>
          <style>
            body { font-family: Arial, sans-serif; background: #f6f7fb; color: #1f2937; margin: 0; padding: 40px; }
            main { max-width: 820px; margin: 0 auto; background: white; border-radius: 14px; padding: 32px; box-shadow: 0 8px 28px rgba(0,0,0,.08); }
            h1 { color: #ee4d2d; margin-top: 0; }
            code { background: #f3f4f6; padding: 3px 7px; border-radius: 5px; }
            li { margin: 10px 0; }
            a { color: #2563eb; }
          </style>
        </head>
        <body>
          <main>
            <h1>跨境代发订单服务运行中</h1>
            <p>FastAPI 服务已启动。可通过下面的接口或 <a href="/docs">/docs</a> 测试。</p>
            <ul>
              <li><code>GET /health</code>：健康检查</li>
              <li><code>POST /api/v1/shopee/order_webhook</code>：Shopee 订单 Webhook</li>
              <li><code>POST /api/v1/aliexpress/order_webhook</code>：AliExpress 订单 Webhook</li>
              <li><code>POST /api/v1/tiktok/order_webhook</code>：TikTok Shop 订单 Webhook</li>
              <li><code>GET /api/v1/purchase/confirm</code>：人工确认链接</li>
              <li><code>POST /api/v1/shopee/tracking/{shopee_order_id}</code>：物流单号回传</li>
            </ul>
            <p>接口文档：<a href="/docs">http://127.0.0.1:8000/docs</a></p>
          </main>
        </body>
        </html>
        """

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    async def process_webhook_payload(
        payload: dict[str, Any], platform: str
    ) -> dict[str, Any]:
        try:
            result = fulfillment_service.process_webhook(payload, platform=platform)
            return {"ok": True, "data": result}
        except OrderParseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            LOGGER.exception("%s order webhook processing failed", platform)
            raise HTTPException(status_code=500, detail=f"Order processing failed: {exc}") from exc

    @app.post("/api/v1/shopee/order_webhook")
    async def shopee_order_webhook(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return await process_webhook_payload(payload, "shopee")

    @app.post("/api/v1/aliexpress/order_webhook")
    async def aliexpress_order_webhook(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return await process_webhook_payload(payload, "aliexpress")

    @app.post("/api/v1/tiktok/order_webhook")
    async def tiktok_order_webhook(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return await process_webhook_payload(payload, "tiktok_shop")

    @app.get("/api/v1/orders")
    async def list_orders(limit: int = 100) -> dict[str, Any]:
        rows = (
            fulfillment_service.store.list_orders(limit=max(1, min(limit, 500)))
            if fulfillment_service.store is not None
            else []
        )
        return {"ok": True, "orders": rows}

    @app.get("/orders", response_class=HTMLResponse)
    async def orders_page(limit: int = 100) -> str:
        rows = (
            fulfillment_service.store.list_orders(limit=max(1, min(limit, 500)))
            if fulfillment_service.store is not None
            else []
        )
        body_rows = "".join(
            "<tr>"
            f"<td>{html.escape(str(row.get('platform', '')))}</td>"
            f"<td>{html.escape(str(row.get('order_id', '')))}</td>"
            f"<td>{html.escape(str(row.get('status', '')))}</td>"
            f"<td>{float(row.get('profit_cny', 0)):.2f}</td>"
            f"<td>{html.escape(str(row.get('confirmation_status', '')))}</td>"
            f"<td>{html.escape(str(row.get('updated_at', '')))}</td>"
            "</tr>"
            for row in rows
        )
        if not body_rows:
            body_rows = '<tr><td colspan="6">暂无订单</td></tr>'
        return f"""
        <!doctype html>
        <html lang="zh-CN">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>订单管理</title>
          <style>
            body {{ font-family: Arial, sans-serif; background:#f6f7fb; margin:0; padding:32px; color:#1f2937; }}
            main {{ max-width:1100px; margin:0 auto; background:white; padding:28px; border-radius:14px; box-shadow:0 8px 28px rgba(0,0,0,.08); }}
            table {{ width:100%; border-collapse:collapse; margin-top:18px; }}
            th, td {{ padding:10px; border-bottom:1px solid #e5e7eb; text-align:left; }}
            th {{ background:#fff1ec; color:#ee4d2d; }}
            a {{ color:#2563eb; }}
          </style>
        </head>
        <body><main>
          <h1>订单管理</h1>
          <p><a href="/">返回首页</a> · <a href="/docs">API 文档</a></p>
          <table>
            <thead><tr><th>平台</th><th>订单号</th><th>利润状态</th><th>利润 CNY</th><th>确认状态</th><th>更新时间</th></tr></thead>
            <tbody>{body_rows}</tbody>
          </table>
        </main></body></html>
        """

    @app.get("/api/v1/purchase/confirm")
    async def purchase_confirm(
        order_id: str, token: str = "", platform: str = "shopee"
    ) -> dict[str, Any]:
        secret = os.environ.get("PURCHASE_CONFIRM_SECRET", "")
        if not verify_confirmation_token(order_id, token, secret):
            raise HTTPException(status_code=403, detail="Invalid confirmation token")
        persisted = fulfillment_service.confirm_purchase(platform, order_id)
        LOGGER.info(
            "Human purchase confirmation received for %s order %s (persisted=%s)",
            platform,
            order_id,
            persisted,
        )
        return {
            "ok": True,
            "platform": platform,
            "order_id": order_id,
            "persisted": persisted,
            "message": "已记录人工确认，请继续在 1688 完成采购。",
        }

    @app.post("/api/v1/shopee/tracking/{shopee_order_id}")
    async def sync_tracking(shopee_order_id: str, tracking_code: str) -> dict[str, Any]:
        try:
            return {"ok": True, "data": sync_tracking_number(shopee_order_id, tracking_code)}
        except Exception as exc:
            LOGGER.exception("Tracking synchronization failed")
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/v1/{platform}/tracking/{order_id}")
    async def sync_platform_tracking(
        platform: str, order_id: str, tracking_code: str
    ) -> dict[str, Any]:
        try:
            from platform_adapters.order_adapters import get_order_adapter

            adapter = get_order_adapter(platform)
            return {"ok": True, "data": adapter.sync_tracking(order_id, tracking_code)}
        except Exception as exc:
            LOGGER.exception("Platform tracking synchronization failed")
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return app


app = create_app()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    uvicorn.run("order_fulfillment:app", host="0.0.0.0", port=8000, reload=False)