"""AliExpress Open Platform product publishing adapter."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any, Mapping

import requests

from category_mapper import CategoryMapper

from .base import (
    PlatformAPIError,
    PlatformPublishResult,
    PlatformPublisher,
    PlatformValidationError,
    coerce_listing,
    round_money,
    listing_bullets_text,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_GATEWAY_URL = "https://api-sg.aliexpress.com/sync"
DEFAULT_PRODUCT_METHOD = "aliexpress.ds.product.create"


def _signature_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def generate_aliexpress_signature(
    app_secret: str,
    parameters: Mapping[str, Any],
    *,
    sign_method: str = "md5",
) -> str:
    """Generate the AliExpress Open Platform parameter signature."""

    if not app_secret:
        raise PlatformValidationError("app_secret cannot be empty")
    normalized_method = sign_method.lower()
    if normalized_method not in {"md5", "hmac-sha256"}:
        raise PlatformValidationError("sign_method must be md5 or hmac-sha256")

    values = {
        key: _signature_value(value)
        for key, value in parameters.items()
        if key != "sign" and value is not None
    }
    concatenated = "".join(f"{key}{values[key]}" for key in sorted(values))
    plain_text = f"{app_secret}{concatenated}{app_secret}"

    if normalized_method == "md5":
        return hashlib.md5(plain_text.encode("utf-8")).hexdigest().upper()
    return hmac.new(
        app_secret.encode("utf-8"), plain_text.encode("utf-8"), hashlib.sha256
    ).hexdigest().upper()


class AliExpressAPIClient:
    """Small signed client for AliExpress Open Platform endpoints."""

    def __init__(
        self,
        *,
        app_key: str,
        app_secret: str,
        access_token: str,
        gateway_url: str = DEFAULT_GATEWAY_URL,
        sign_method: str = "md5",
        session: requests.Session | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not app_key or not app_secret or not access_token:
            raise PlatformValidationError("app_key, app_secret and access_token are required")
        self.app_key = app_key
        self.app_secret = app_secret
        self.access_token = access_token
        self.gateway_url = gateway_url
        self.sign_method = sign_method
        self.session = session or requests.Session()
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "AliExpressAPIClient":
        required = {
            "ALIEXPRESS_APP_KEY": os.environ.get("ALIEXPRESS_APP_KEY"),
            "ALIEXPRESS_APP_SECRET": os.environ.get("ALIEXPRESS_APP_SECRET"),
            "ALIEXPRESS_ACCESS_TOKEN": os.environ.get("ALIEXPRESS_ACCESS_TOKEN"),
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise PlatformValidationError(
                "Missing AliExpress environment variables: " + ", ".join(missing)
            )
        return cls(
            app_key=str(required["ALIEXPRESS_APP_KEY"]),
            app_secret=str(required["ALIEXPRESS_APP_SECRET"]),
            access_token=str(required["ALIEXPRESS_ACCESS_TOKEN"]),
            gateway_url=os.environ.get("ALIEXPRESS_GATEWAY_URL", DEFAULT_GATEWAY_URL),
            sign_method=os.environ.get("ALIEXPRESS_SIGN_METHOD", "md5"),
            timeout=float(os.environ.get("ALIEXPRESS_TIMEOUT", "30")),
        )

    def upload_image_from_url(
        self,
        image_url: str,
        *,
        method: str | None = None,
    ) -> dict[str, Any]:
        """Upload/register a public image URL through the configured media method."""

        if not str(image_url).startswith(("http://", "https://")):
            raise PlatformValidationError("image_url must be an absolute HTTP(S) URL")
        upload_method = method or os.environ.get(
            "ALIEXPRESS_UPLOAD_METHOD", "aliexpress.media.upload"
        )
        return self.call(upload_method, {"image_url": image_url})

    def call(self, method: str, business_params: dict[str, Any]) -> dict[str, Any]:
        timestamp = str(int(time.time() * 1000))
        params: dict[str, Any] = {
            "method": method,
            "app_key": self.app_key,
            "session": self.access_token,
            "timestamp": timestamp,
            "format": "json",
            "v": "2.0",
            "sign_method": self.sign_method,
            "param_json": json.dumps(
                business_params, ensure_ascii=False, separators=(",", ":")
            ),
        }
        params["sign"] = generate_aliexpress_signature(
            self.app_secret, params, sign_method=self.sign_method
        )

        try:
            response = self.session.post(self.gateway_url, data=params, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise PlatformAPIError(f"AliExpress request failed: {exc}") from exc
        except ValueError as exc:
            raise PlatformAPIError("AliExpress returned a non-JSON response") from exc

        if not isinstance(data, dict):
            raise PlatformAPIError(
                "AliExpress returned an unexpected response shape",
                response_data={"raw": data},
            )
        error_response = data.get("error_response") or data.get("errorResponse")
        if error_response:
            message = error_response.get("msg") or error_response.get("sub_msg") or str(error_response)
            raise PlatformAPIError(f"AliExpress API error: {message}", response_data=data)
        return data

def build_aliexpress_product_payload(listing_data: dict[str, Any]) -> dict[str, Any]:
    """Map a standard listing to an AliExpress product creation request."""

    listing = coerce_listing(listing_data)
    listing_data = CATEGORY_MAPPER.enrich("aliexpress", listing_data)
    category_id = listing_data.get("category_id")
    if category_id in (None, ""):
        raise PlatformValidationError(
            "category_id is required for AliExpress publishing and must be numeric"
        )

    image_urls = listing_data.get("image_urls") or listing_data.get("images") or []
    if isinstance(image_urls, str):
        image_urls = [image_urls]
    image_urls = [str(url).strip() for url in image_urls if str(url).strip()]
    if not image_urls:
        raise PlatformValidationError(
            "image_urls is required; AliExpress normally accepts public HTTPS image URLs"
        )

    conversion_rate = float(listing_data.get("sgd_to_target_currency_rate", 1.0))
    currency_code = str(listing_data.get("currency_code") or "USD").upper()
    default_stock = max(1, int(listing_data.get("normal_stock", listing_data.get("stock", 10))))
    description = f"{listing_bullets_text(listing)}\n\n{listing.description}".strip()

    sku_list: list[dict[str, Any]] = []
    for index, variation in enumerate(listing.variations):
        sku_list.append(
            {
                "sku_code": variation.en_spec,
                "sku_attributes": [
                    {
                        "attribute_name": str(listing_data.get("variation_name") or "Style"),
                        "attribute_value": variation.en_spec,
                    }
                ],
                "price": round_money(variation.target_price_sgd * conversion_rate),
                "currency_code": currency_code,
                "inventory": default_stock,
                "sku_index": index,
            }
        )

    payload: dict[str, Any] = {
        "product_title": listing.title,
        "product_description": description,
        "category_id": int(category_id),
        "images": [{"image_url": url, "image_type": "main"} for url in image_urls],
        "sku_list": sku_list,
        "keywords": ", ".join(listing.tags),
        "package_weight": float(listing_data.get("weight", listing_data.get("package_weight", 0.1))),
        "product_status": str(listing_data.get("product_status") or "draft"),
    }
    for field in ("package_length", "package_width", "package_height"):
        if listing_data.get(field) is not None:
            payload[field] = float(listing_data[field])
    return payload


class AliExpressPublisher(PlatformPublisher):
    """AliExpress adapter implementing the common publisher interface."""

    platform = "aliexpress"

    def __init__(
        self,
        *,
        client: AliExpressAPIClient | None = None,
        product_method: str = DEFAULT_PRODUCT_METHOD,
    ) -> None:
        self.client = client or AliExpressAPIClient.from_env()
        self.product_method = product_method

    def build_payload(self, listing_data: dict[str, Any]) -> dict[str, Any]:
        return build_aliexpress_product_payload(listing_data)

    def publish(self, listing_data: dict[str, Any]) -> PlatformPublishResult:
        payload = self.build_payload(listing_data)
        response = self.client.call(self.product_method, payload)
        result_data = response.get("aliexpress_ds_product_create_response") or response
        external_id = None
        if isinstance(result_data, dict):
            external_id = (
                result_data.get("product_id")
                or result_data.get("item_id")
                or result_data.get("id")
            )
        return PlatformPublishResult(
            platform=self.platform,
            success=True,
            status="DRAFT_OR_OFFLINE",
            external_id=str(external_id) if external_id is not None else None,
            raw_response=response,
        )


def publish_aliexpress_product(
    listing_data: dict[str, Any],
    *,
    client: AliExpressAPIClient | None = None,
) -> PlatformPublishResult:
    """Convenience helper for publishing one AliExpress product."""

    return AliExpressPublisher(client=client).publish(listing_data)
CATEGORY_MAPPER = CategoryMapper()
