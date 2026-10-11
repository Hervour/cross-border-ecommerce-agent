"""Local platform category and attribute mapping."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

DEFAULT_CATEGORY_PATH = Path(__file__).resolve().parent / "data" / "category_mapping.json"


def _tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", value.lower()) if len(token) > 1}


class CategoryMapper:
    """Resolve a platform category ID from local keyword mappings."""

    def __init__(self, mapping_path: str | Path = DEFAULT_CATEGORY_PATH) -> None:
        self.mapping_path = Path(mapping_path)
        payload = json.loads(self.mapping_path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("category mapping must contain an object")
        self.mappings: dict[str, list[dict[str, Any]]] = {
            str(platform).lower(): list(records) if isinstance(records, list) else []
            for platform, records in payload.items()
        }

    def resolve(self, platform: str, category_text: str) -> dict[str, Any] | None:
        platform_key = platform.strip().lower().replace("-", "_")
        source_tokens = _tokens(category_text)
        best: dict[str, Any] | None = None
        best_score = 0
        for record in self.mappings.get(platform_key, []):
            keywords = record.get("keywords") or []
            score = len(source_tokens.intersection(_tokens(" ".join(map(str, keywords)))))
            if score > best_score:
                best_score = score
                best = record
        return best

    def enrich(self, platform: str, listing_data: dict[str, Any]) -> dict[str, Any]:
        result = dict(listing_data)
        category_text = str(result.get("category") or result.get("title") or "")
        matched = self.resolve(platform, category_text)
        if not matched:
            return result
        result.setdefault("category_id", matched.get("category_id"))
        attributes = matched.get("attributes")
        if platform == "tiktok_shop":
            result.setdefault("product_attributes", attributes or [])
        elif attributes:
            result.setdefault("attributes", attributes)
        return result