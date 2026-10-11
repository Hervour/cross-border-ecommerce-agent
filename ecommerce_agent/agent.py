"""Core DeepSeek-powered agent that transforms supplier data into a listing."""

from __future__ import annotations

import json
import os
from typing import Any

from dotenv import load_dotenv

from openai import OpenAI

from .models import ListingDraft, ProductVariation, StandardProductListing
from .pricing import calculate_selling_price

load_dotenv()

DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"

SYSTEM_PROMPT = """
You are a senior cross-border e-commerce sourcing and marketplace listing expert.
Transform raw Chinese supplier data into a concise, accurate, English-only Shopee listing.

NON-NEGOTIABLE RULES:
1. Output English only. Do not add Chinese words, pinyin, conversational filler, or internal reasoning.
2. Use only facts present in the input. Never invent certifications, materials, dimensions, load limits, warranties, or performance claims.
3. Remove supplier-facing noise such as factory names, seller IDs, wholesale slogans, unrelated promotions, original Chinese marketing copy, and duplicated specifications.
4. Preserve every valid SKU variation and keep its actual numeric supplier cost in CNY.
5. Do not calculate or output a selling price. Pricing is calculated by deterministic Python code after your response.
6. Use natural marketplace English, not literal machine translation.

FIELD RULES:
- title: 80-120 characters including spaces. Front-load the product type, key material/feature, and important use cases. No ALL CAPS and no keyword stuffing.
- bullet_points: exactly 5 items. Start every item with a relevant Emoji. Each item must combine a practical benefit with a concrete specification when available.
- description: structured Markdown with short headings and concise bullet lists. Include core features, specifications, suitable scenarios, and package contents only when known.
- category: propose the most relevant overseas marketplace category or category path.
- tags: 5-15 distinct, lowercase English search keywords without the # symbol.
- variations.original_spec: retain the supplier's original variation specification accurately.
- variations.en_spec: translate and clean the variation into a natural English SKU name.
- variations.cost_cny: numeric cost for that exact SKU. Use the supplier value; do not blend prices across SKUs.

If a fact is missing, omit it rather than guessing.
""".strip()


class ListingAgent:
    """Convert raw supplier data into `StandardProductListing` using DeepSeek.

    The LLM handles translation, enrichment, categorization, and copywriting.
    Pricing is always recalculated in Python and overwrites any model-side value.
    """

    def __init__(
        self,
        *,
        client: Any | None = None,
        model: str | None = None,
        base_url: str | None = None,
        profit_margin: float = 0.35,
        exchange_rate_cny_to_sgd: float = 0.19,
        estimated_shipping_cny: float = 10.0,
        platform_commission_rate: float = 0.10,
    ) -> None:
        self.model = model or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
        self.base_url = (
            base_url or os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL
        )

        self.profit_margin = profit_margin
        self.exchange_rate_cny_to_sgd = exchange_rate_cny_to_sgd
        self.estimated_shipping_cny = estimated_shipping_cny
        self.platform_commission_rate = platform_commission_rate

        # Client injection keeps the agent unit-testable without an API key.
        if client is not None:
            self.client = client
            return

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError(
                "OPENAI_API_KEY is not set. Set it in the environment before "
                "creating ListingAgent."
            )

        self.client = OpenAI(api_key=api_key, base_url=self.base_url)

    def transform(self, raw_data: dict[str, Any]) -> StandardProductListing:
        """Transform one module-1 payload into a publish-ready listing object."""

        if not isinstance(raw_data, dict):
            raise TypeError("raw_data must be a dict produced by module 1")
        if not raw_data:
            raise ValueError("raw_data cannot be empty")

        draft = self._request_structured_draft(raw_data)

        variations = [
            ProductVariation(
                original_spec=item.original_spec,
                en_spec=item.en_spec,
                cost_cny=item.cost_cny,
                target_price_sgd=calculate_selling_price(
                    cost_cny=item.cost_cny,
                    profit_margin=self.profit_margin,
                    exchange_rate_cny_to_sgd=self.exchange_rate_cny_to_sgd,
                    estimated_shipping_cny=self.estimated_shipping_cny,
                    platform_commission_rate=self.platform_commission_rate,
                ),
            )
            for item in draft.variations
        ]

        return StandardProductListing(
            title=draft.title,
            bullet_points=draft.bullet_points,
            description=draft.description,
            category=draft.category,
            tags=draft.tags,
            variations=variations,
        )

    def _request_structured_draft(self, raw_data: dict[str, Any]) -> ListingDraft:
        raw_json = json.dumps(raw_data, ensure_ascii=False, indent=2, default=str)
        schema_json = json.dumps(
            ListingDraft.model_json_schema(), ensure_ascii=False, separators=(",", ":")
        )
        user_input = (
            "Convert the following module-1 supplier record into the required JSON object. "
            "Return valid JSON only. The JSON Schema is:\n"
            f"{schema_json}\n\n"
            f"RAW_SUPPLIER_DATA:\n{raw_json}"
        )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_input},
        ]

        mode = os.environ.get("OPENAI_STRUCTURED_MODE", "json_object").strip().lower()
        if mode == "json_schema":
            response = self.client.beta.chat.completions.parse(
                model=self.model,
                messages=messages,
                response_format=ListingDraft,
            )
            message = response.choices[0].message
            refusal = getattr(message, "refusal", None)
            if refusal:
                raise RuntimeError(f"DeepSeek refused the listing request: {refusal}")
            draft = message.parsed
            if draft is None:
                raise RuntimeError("DeepSeek returned no parsed structured output")
            return draft

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("DeepSeek returned empty JSON output")
        try:
            return ListingDraft.model_validate_json(content)
        except Exception as exc:
            raise RuntimeError(f"DeepSeek returned invalid listing JSON: {exc}") from exc