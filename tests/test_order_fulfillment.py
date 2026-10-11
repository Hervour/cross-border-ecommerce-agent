from __future__ import annotations

import unittest
from typing import Any

from fastapi.testclient import TestClient

from order_fulfillment import (
    OrderFulfillmentService,
    PurchaseNotifier,
    create_app,
    parse_shopee_order_payload,
    sync_tracking_number,
)


class _FakeResponse:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = data or {"ok": True}
        self.status_code = 200
        self.text = "ok"

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.data


class _RecordingPostSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"url": url, "kwargs": kwargs})
        return _FakeResponse()


class _FakeShopeeClient:
    def __init__(self) -> None:
        self.path = ""
        self.payload: dict[str, Any] = {}

    def call_api(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.path = path
        self.payload = payload
        return {"order_sn": payload["order_sn"], "status": "PROCESSED"}


def _webhook_payload(paid_amount: float = 100.0, sku: str = "SHOPEE-SKU-CAMP-BEIGE") -> dict[str, Any]:
    return {
        "code": 3,
        "shop_id": 123456,
        "data": {
            "ordersn": "SHOPEE-ORDER-001",
            "status": "READY_TO_SHIP",
            "currency": "CNY",
            "paid_amount": paid_amount,
            "item_list": [{"model_sku": sku, "item_name": "Portable Camping Chair", "quantity": 1}],
            "recipient_address": {
                "name": "Buyer One",
                "phone": "0800000000",
                "city": "Singapore",
                "country": "SG",
                "zipcode": "018956",
                "full_address": "1 Marina Boulevard, Singapore 018956",
                "address_type": "overseas",
            },
        },
    }

class OrderParserTests(unittest.TestCase):
    def test_parse_common_shopee_webhook_shape(self) -> None:
        order = parse_shopee_order_payload(_webhook_payload())

        self.assertEqual(order.shopee_order_id, "SHOPEE-ORDER-001")
        self.assertEqual(order.shop_id, 123456)
        self.assertEqual(order.items[0].sku_id, "SHOPEE-SKU-CAMP-BEIGE")
        self.assertEqual(order.delivery_address.city, "Singapore")
        self.assertEqual(order.paid_amount, 100.0)


class FulfillmentServiceTests(unittest.TestCase):
    def test_ready_for_purchase_and_notification(self) -> None:
        captured: list[dict[str, Any]] = []
        service = OrderFulfillmentService(
            notifier=lambda details: captured.append(details) or {"sent": True}
        )

        result = service.process_webhook(_webhook_payload())

        self.assertEqual(result["status"], "READY_FOR_PURCHASE")
        self.assertEqual(result["total_purchase_cost_cny"], 68.0)
        self.assertEqual(result["estimated_profit_cny"], 32.0)
        self.assertFalse(result["is_anomaly"])
        self.assertEqual(len(captured), 1)

    def test_negative_profit_marks_anomaly(self) -> None:
        service = OrderFulfillmentService(notifier=lambda details: {"sent": True})
        result = service.process_webhook(_webhook_payload(paid_amount=50.0))

        self.assertEqual(result["status"], "ANOMALY_LOW_PROFIT")
        self.assertTrue(result["is_anomaly"])
        self.assertLess(result["estimated_profit_cny"], 0)

    def test_missing_sku_mapping_marks_anomaly(self) -> None:
        service = OrderFulfillmentService(notifier=lambda details: {"sent": True})
        result = service.process_webhook(_webhook_payload(sku="UNKNOWN-SKU"))

        self.assertEqual(result["status"], "ANOMALY_MAPPING_MISSING")
        self.assertEqual(result["missing_mappings"], ["UNKNOWN-SKU"])


class NotificationTests(unittest.TestCase):
    def test_notifier_sends_all_channels_with_confirmation_button(self) -> None:
        session = _RecordingPostSession()
        notifier = PurchaseNotifier(
            wecom_webhook_url="https://wecom.example/webhook",
            feishu_webhook_url="https://feishu.example/webhook",
            telegram_bot_token="telegram-token",
            telegram_chat_id="telegram-chat",
            confirmation_base_url="https://agent.example/api/v1/purchase/confirm",
            confirmation_secret="confirm-secret",
            session=session,  # type: ignore[arg-type]
        )
        order_details = {
            "shopee_order_id": "SHOPEE-ORDER-001",
            "status": "READY_FOR_PURCHASE",
            "estimated_profit_cny": 32.0,
            "supplier_links": [
                {
                    "sku_id": "SHOPEE-SKU-CAMP-BEIGE",
                    "supplier_url": "https://detail.1688.com/offer/123.html",
                    "supplier_spec": "米白色 标准款",
                    "total_cost_cny": 68.0,
                }
            ],
            "delivery_address": {
                "recipient_name": "Buyer One",
                "phone": "0800000000",
                "full_address": "Singapore Address",
                "address_type": "overseas",
            },
        }

        result = notifier.send(order_details)

        self.assertTrue(result["sent"])
        self.assertEqual(len(session.calls), 3)
        telegram_payload = session.calls[2]["kwargs"]["json"]
        self.assertEqual(telegram_payload["chat_id"], "telegram-chat")
        self.assertIn("inline_keyboard", telegram_payload["reply_markup"])
        feishu_payload = session.calls[1]["kwargs"]["json"]
        self.assertIn("确认代采", str(feishu_payload))


class TrackingTests(unittest.TestCase):
    def test_tracking_number_is_sent_to_shopee_ship_order(self) -> None:
        client = _FakeShopeeClient()

        result = sync_tracking_number(
            "SHOPEE-ORDER-001",
            "SF123456789",
            client=client,  # type: ignore[arg-type]
            logistics_id=88,
        )

        self.assertEqual(result["status"], "PROCESSED")
        self.assertTrue(client.path.endswith("/api/v2/logistics/ship_order"))
        self.assertEqual(client.payload["order_sn"], "SHOPEE-ORDER-001")
        self.assertEqual(client.payload["non_integrated"]["tracking_number"], "SF123456789")
        self.assertEqual(client.payload["logistics_id"], 88)


class WebhookRouteTests(unittest.TestCase):
    def test_root_page_is_available(self) -> None:
        app = create_app(OrderFulfillmentService(notifier=lambda details: {"sent": True}))
        client = TestClient(app)

        response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("跨境代发订单服务运行中", response.text)

    def test_webhook_route_returns_fulfillment_decision(self) -> None:
        service = OrderFulfillmentService(notifier=lambda details: {"sent": True})
        app = create_app(service)
        client = TestClient(app)

        response = client.post("/api/v1/shopee/order_webhook", json=_webhook_payload())

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["data"]["status"], "READY_FOR_PURCHASE")


if __name__ == "__main__":
    unittest.main()