"""Pydantic models used by the product listing agent."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProductVariation(BaseModel):
    """A normalized English SKU with a deterministic target price."""

    model_config = ConfigDict(extra="forbid")

    original_spec: str = Field(
        min_length=1,
        description="Original supplier specification, usually in Chinese.",
    )
    en_spec: str = Field(
        min_length=1,
        description="Clear English specification suitable for Shopee variations.",
    )
    cost_cny: float = Field(
        ge=0,
        description="Supplier purchase cost for this SKU in CNY.",
    )
    target_price_sgd: float = Field(
        ge=0.01,
        description="Calculated selling price for this SKU in SGD.",
    )


class StandardProductListing(BaseModel):
    """Final normalized listing consumed by the Shopee publishing module."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(
        min_length=80,
        max_length=120,
        description="English SEO title between 80 and 120 characters.",
    )
    bullet_points: list[str] = Field(
        min_length=5,
        max_length=5,
        description="Exactly five selling points with relevant Emoji.",
    )
    description: str = Field(
        min_length=50,
        description="Structured Markdown product description.",
    )
    category: str = Field(
        min_length=2,
        description="Recommended overseas marketplace category path.",
    )
    tags: list[str] = Field(
        min_length=5,
        max_length=15,
        description="Search keywords suitable for marketplace tags.",
    )
    variations: list[ProductVariation] = Field(
        min_length=1,
        description="Normalized SKU list with original and English specifications.",
    )

    @field_validator("title")
    @classmethod
    def validate_title_length(cls, value: str) -> str:
        value = value.strip()
        if not 80 <= len(value) <= 120:
            raise ValueError("title must contain 80-120 characters")
        return value

    @field_validator("bullet_points")
    @classmethod
    def validate_bullet_points(cls, value: list[str]) -> list[str]:
        if len(value) != 5:
            raise ValueError("bullet_points must contain exactly 5 items")
        if any(not item.strip() for item in value):
            raise ValueError("bullet_points cannot contain empty items")
        return [item.strip() for item in value]

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: list[str]) -> list[str]:
        tags = [item.strip() for item in value if item.strip()]
        tags = list(dict.fromkeys(tags))
        if len(tags) < 5:
            raise ValueError("tags must contain at least 5 unique non-empty keywords")
        return tags


class ListingDraftVariation(BaseModel):
    """LLM-facing SKU model. Price is intentionally calculated in Python."""

    model_config = ConfigDict(extra="forbid")

    original_spec: str = Field(min_length=1)
    en_spec: str = Field(min_length=1)
    cost_cny: float = Field(ge=0)


class ListingDraft(BaseModel):
    """Structured Output schema sent to the LLM."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(
        min_length=80,
        max_length=120,
        description="English SEO title with 80-120 characters.",
    )
    bullet_points: list[str] = Field(
        min_length=5,
        max_length=5,
        description="Exactly five concise selling points, each starting with an Emoji.",
    )
    description: str = Field(
        min_length=50,
        description="Structured Markdown detail section with specifications.",
    )
    category: str = Field(
        min_length=2,
        description="Recommended Shopee category or category path.",
    )
    tags: list[str] = Field(
        min_length=5,
        max_length=15,
        description="English search keywords.",
    )
    variations: list[ListingDraftVariation] = Field(
        min_length=1,
        description="Normalized SKU data; never invent a price.",
    )