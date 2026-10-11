from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from ecommerce_agent import (
    ListingAgent,
    ListingDraft,
    ListingDraftVariation,
    calculate_selling_price,
)


class _FakeCompletions:
    def __init__(self, draft: ListingDraft) -> None:
        self.draft = draft
        self.captured: dict[str, Any] = {}

    def parse(self, **kwargs: Any) -> SimpleNamespace:
        self.captured = kwargs
        message = SimpleNamespace(parsed=self.draft, refusal=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.captured = kwargs
        content = json.dumps(self.draft.model_dump(), ensure_ascii=False)
        message = SimpleNamespace(content=content, parsed=self.draft, refusal=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _FakeClient:
    def __init__(self, draft: ListingDraft) -> None:
        self.completions = _FakeCompletions(draft)
        self.chat = SimpleNamespace(completions=self.completions)
        self.beta = SimpleNamespace(
            chat=SimpleNamespace(completions=self.completions)
        )


def _sample_draft() -> ListingDraft:
    return ListingDraft(
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
        description=(
            "## Portable Outdoor Camping Chair\n\n"
            "A lightweight folding chair designed for convenient outdoor seating.\n\n"
            "### Specifications\n"
            "- Frame: Aluminum alloy\n"
            "- Fabric: 600D Oxford\n"
            "- Open size: 52 x 46 x 68 cm\n"
            "- Folded size: 35 x 12 x 12 cm"
        ),
        category="Sports & Outdoors > Camping & Hiking > Camping Furniture > Chairs",
        tags=["camping chair", "folding chair", "outdoor chair", "moon chair", "beach chair"],
        variations=[
            ListingDraftVariation(
                original_spec="米白色 标准款 铝合金支架",
                en_spec="Beige Standard Aluminum Frame",
                cost_cny=58.0,
            ),
            ListingDraftVariation(
                original_spec="军绿色 加宽款 带侧边网袋",
                en_spec="Army Green Wide with Side Mesh Pocket",
                cost_cny=69.0,
            ),
        ],
    )


class PricingTests(unittest.TestCase):
    def test_selling_price_reserves_commission_and_net_margin(self) -> None:
        price = calculate_selling_price(cost_cny=58.0)
        self.assertEqual(price, 23.50)

    def test_invalid_margin_combination_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            calculate_selling_price(cost_cny=58.0, profit_margin=0.95)


class ListingAgentTests(unittest.TestCase):
    def test_transform_uses_json_object_mode_and_recalculates_prices(self) -> None:
        draft = _sample_draft()
        client = _FakeClient(draft)
        agent = ListingAgent(client=client, model="test-model")

        listing = agent.transform({"title": "露营椅", "price": 58})

        self.assertEqual(len(listing.variations), 2)
        self.assertEqual(listing.variations[0].target_price_sgd, 23.50)
        self.assertEqual(listing.variations[1].target_price_sgd, 27.30)
        self.assertEqual(client.completions.captured["model"], "test-model")
        self.assertEqual(client.completions.captured["response_format"], {"type": "json_object"})

        plain_data = listing.model_dump()
        self.assertIsInstance(plain_data["variations"][0], dict)

    def test_constructor_uses_environment_key_and_custom_endpoint(self) -> None:
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True),
            patch("ecommerce_agent.agent.OpenAI") as openai_class,
        ):
            sentinel_client = object()
            openai_class.return_value = sentinel_client

            agent = ListingAgent(
                model="custom-deepseek-model",
                base_url="https://custom.example/v1",
            )

            self.assertIs(agent.client, sentinel_client)
            openai_class.assert_called_once_with(
                api_key="test-key",
                base_url="https://custom.example/v1",
            )

    def test_constructor_defaults_to_deepseek(self) -> None:
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True),
            patch("ecommerce_agent.agent.OpenAI") as openai_class,
        ):
            agent = ListingAgent()

            self.assertEqual(agent.model, "deepseek-flash")
            self.assertEqual(agent.base_url, "https://api.deepseek.com")
            openai_class.assert_called_once_with(
                api_key="test-key",
                base_url="https://api.deepseek.com",
            )


if __name__ == "__main__":
    unittest.main()