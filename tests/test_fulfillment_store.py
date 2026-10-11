from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from fulfillment_store import FulfillmentStore
from order_fulfillment import OrderFulfillmentService, create_app


def _payload() -> dict[str, Any]:
    return {
        "code": 3,
        "shop_id": 123456,
        "data": {
            "ordersn": "DB-ORDER-001",
            "status": "READY_TO_SHIP",
            "currency": "CNY",
            "paid_amount": 100,
            "item_list": [{"model_sku": "SHOPEE-SKU-CAMP-BEIGE", "quantity": 1}],
            "recipient_address": {
                "name": "Buyer",
                "phone": "0800000000",
                "city": "Singapore",
                "country": "SG",
                "full_address": "1 Marina Boulevard",
            },
        },
    }


class FulfillmentStoreTests(unittest.TestCase):
    def test_store_deduplicates_and_persists_orders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = FulfillmentStore(Path(temp_dir) / "fulfillment.db")
            alerts: list[dict[str, Any]] = []
            service = OrderFulfillmentService(
                store=store,
                notifier=lambda details: alerts.append(details) or {"sent": True},
            )

            first = service.process_webhook(_payload())
            second = service.process_webhook(_payload())

            self.assertEqual(first["status"], "READY_FOR_PURCHASE")
            self.assertTrue(second["duplicate"])
            self.assertEqual(len(alerts), 1)
            self.assertEqual(len(store.list_orders()), 1)
            self.assertTrue(service.confirm_purchase("shopee", "DB-ORDER-001"))
            saved = store.get_order("shopee", "DB-ORDER-001")
            self.assertIsNotNone(saved)
            self.assertEqual(saved["confirmation_status"], "CONFIRMED")

    def test_orders_dashboard_reads_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = FulfillmentStore(Path(temp_dir) / "fulfillment.db")
            service = OrderFulfillmentService(
                store=store, notifier=lambda details: {"sent": True}
            )
            client = TestClient(create_app(service))

            self.assertEqual(
                client.post("/api/v1/shopee/order_webhook", json=_payload()).status_code,
                200,
            )
            response = client.get("/orders")
            api_response = client.get("/api/v1/orders")

            self.assertEqual(response.status_code, 200)
            self.assertIn("DB-ORDER-001", response.text)
            self.assertEqual(api_response.json()["orders"][0]["order_id"], "DB-ORDER-001")


if __name__ == "__main__":
    unittest.main()