"""Common interfaces for marketplace publishing adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from pydantic import ValidationError

from ecommerce_agent.models import StandardProductListing


class PlatformAdapterError(Exception):
    """Base error raised by marketplace adapters."""


class PlatformValidationError(PlatformAdapterError):
    """Raised when a listing cannot be mapped to a platform payload."""


class PlatformAPIError(PlatformAdapterError):
    """Raised when a marketplace API request fails."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data or {}


@dataclass(slots=True)
class PlatformPublishResult:
    platform: str
    success: bool
    status: str
    external_id: str | None = None
    raw_response: dict[str, Any] = field(default_factory=dict)


def coerce_listing(listing_data: dict[str, Any]) -> StandardProductListing:
    if not isinstance(listing_data, dict):
        raise PlatformValidationError("listing_data must be a dictionary")

    fields = ("title", "bullet_points", "description", "category", "tags", "variations")
    try:
        return StandardProductListing.model_validate(
            {key: listing_data[key] for key in fields if key in listing_data}
        )
    except (ValidationError, KeyError) as exc:
        raise PlatformValidationError(f"Invalid StandardProductListing: {exc}") from exc


def listing_bullets_text(listing: StandardProductListing) -> str:
    return "\n".join(f"- {item}" for item in listing.bullet_points)


def round_money(value: float) -> float:
    """Round a monetary value to two decimal places using half-up semantics."""

    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class PlatformPublisher(ABC):
    """Platform-neutral publisher interface used by orchestration code."""

    platform: str

    @abstractmethod
    def build_payload(self, listing_data: dict[str, Any]) -> dict[str, Any]:
        """Map a standard listing to the platform request body."""

    @abstractmethod
    def publish(self, listing_data: dict[str, Any]) -> PlatformPublishResult:
        """Submit a listing to the platform."""