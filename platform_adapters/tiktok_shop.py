"""TikTok Shop product publishing adapter."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path
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

DEFAULT_BASE_URL = "https://open-api.tiktokglobalshop.com"
DEFAULT_PRODUCT_PATH = "/product/202309/products"
DEFAULT_PRODUCT_VERSION = "202309"
SIGN_EXCLUDED_KEYS = {"app_secret", "token", "access_token", "sign"}


def generate_tiktok_shop_signature(
    app_secret: str,
    path: str,
    query_params: Mapping[str, Any],
    body: Mapping[str, Any] | None = None,
) -> str:
    """Generate a TikTok Shop Partner API HMAC-SHA256 signature."""

    if not app_secret:
        raise PlatformValidationError("app_secret cannot be empty")
    if not path.startswith("/"):
        raise PlatformValidationError("TikTok Shop path must start with '/'")

    values = {
        key: value
        for key, value in query_params.items()
        if key not in SIGN_EXCLUDED_KEYS and value is not None
    }
    if "timestamp" not in values:
        values["timestamp"] = int(time.time()) - 100

    concatenated = "".join(f"{key}{values[key]}" for key in sorted(values))
    input_text = f"{path}{concatenated}"
    if body:
        input_text += json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    plain_text = f"{app_secret}{input_text}{app_secret}"
    return hmac.new(
        app_secret.encode("utf-8"), plain_text.encode("utf-8"), hashlib.sha256
    ).hexdigest()


class TikTokShopAPIClient:
    """Signed client for TikTok Shop Partner Open API."""

    def __init__(
        self,
        *,
        app_key: str,
        app_secret: str,
        access_token: str,
        shop_cipher: str,
        base_url: str = DEFAULT_BASE_URL,
        session: requests.Session | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not app_key or not app_secret or not access_token or not shop_cipher:
            raise PlatformValidationError(
                "app_key, app_secret, access_token and shop_cipher are required"
            )
        self.app_key = app_key
        self.app_secret = app_secret
        self.access_token = access_token
        self.shop_cipher = shop_cipher
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "TikTokShopAPIClient":
        required = {
            "TIKTOK_SHOP_APP_KEY": os.environ.get("TIKTOK_SHOP_APP_KEY"),
            "TIKTOK_SHOP_APP_SECRET": os.environ.get("TIKTOK_SHOP_APP_SECRET"),
            "TIKTOK_SHOP_ACCESS_TOKEN": os.environ.get("TIKTOK_SHOP_ACCESS_TOKEN"),
            "TIKTOK_SHOP_CIPHER": os.environ.get("TIKTOK_SHOP_CIPHER"),
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise PlatformValidationError(
                "Missing TikTok Shop environment variables: " + ", ".join(missing)
            )
        return cls(
            app_key=str(required["TIKTOK_SHOP_APP_KEY"]),
            app_secret=str(required["TIKTOK_SHOP_APP_SECRET"]),
            access_token=str(required["TIKTOK_SHOP_ACCESS_TOKEN"]),
            shop_cipher=str(required["TIKTOK_SHOP_CIPHER"]),
            base_url=os.environ.get("TIKTOK_SHOP_BASE_URL", DEFAULT_BASE_URL),
            timeout=float(os.environ.get("TIKTOK_SHOP_TIMEOUT", "30")),
        )

    def upload_image(
        self,
        image_path: str | Path,
        *,
        path: str | None = None,
        image_type: str = "MAIN_IMAGE",
    ) -> dict[str, Any]:
        """Upload a local image using TikTok Shop's multipart media endpoint."""

        image = Path(image_path)
        if not image.exists():
            raise PlatformValidationError(f"image file does not exist: {image}")
        upload_path = path or os.environ.get(
            "TIKTOK_SHOP_IMAGE_UPLOAD_PATH", "/product/202309/images/upload"
        )
        params: dict[str, Any] = {
            "app_key": self.app_key,
            "shop_cipher": self.shop_cipher,
            "timestamp": int(time.time()) - 100,
            "version": DEFAULT_PRODUCT_VERSION,
        }
        params["sign"] = generate_tiktok_shop_signature(
            self.app_secret, upload_path, params, body=None
        )
        params["access_token"] = self.access_token
        with image.open("rb") as file_handle:
            try:
                response = self.session.post(
                    f"{self.base_url}{upload_path}",
                    params=params,
                    data={"data": json.dumps({"image_type": image_type})},
                    files={"image": (image.name, file_handle, "application/octet-stream")},
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json()
            except requests.RequestException as exc:
                raise PlatformAPIError(f"TikTok Shop image upload failed: {exc}") from exc
            except ValueError as exc:
                raise PlatformAPIError("TikTok Shop returned a non-JSON upload response") from exc
        if not isinstance(data, dict):
            raise PlatformAPIError("TikTok Shop returned an unexpected upload response")
        if data.get("code") not in (None, 0, "0"):
            raise PlatformAPIError(
                str(data.get("message") or "TikTok Shop image upload failed"),
                response_data=data,
            )
        return data
    def call(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "app_key": self.app_key,
            "shop_cipher": self.shop_cipher,
            "timestamp": int(time.time()) - 100,
        }
        if query_params:
            params.update(query_params)
        if path.startswith("/product/"):
            params.setdefault("version", DEFAULT_PRODUCT_VERSION)

        body_text = (
            json.dumps(body, ensure_ascii=False, separators=(",", ":"))
            if body is not None
            else None
        )
        params["sign"] = generate_tiktok_shop_signature(
            self.app_secret, path, params, body
        )
        params["access_token"] = self.access_token

        try:
            response = self.session.request(
                method.upper(),
                f"{self.base_url}{path}",
                params=params,
                data=body_text,
                headers={"Content-Type": "application/json"} if body_text else None,
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise PlatformAPIError(f"TikTok Shop request failed: {exc}") from exc
        except ValueError as exc:
            raise PlatformAPIError("TikTok Shop returned a non-JSON response") from exc

        if not isinstance(data, dict):
            raise PlatformAPIError("TikTok Shop returned an unexpected response shape")
        if data.get("code") not in (None, 0, "0"):
            raise PlatformAPIError(
                str(data.get("message") or "TikTok Shop API request failed"),
                response_data=data,
            )
        return data

def _plain_text(value: str) -> str:
    return " ".join(value.replace("\n", " ").split())


def build_tiktok_shop_product_payload(listing_data: dict[str, Any]) -> dict[str, Any]:
    """Map a standard listing to a TikTok Shop product create request."""

    listing = coerce_listing(listing_data)
    listing_data = CATEGORY_MAPPER.enrich("tiktok_shop", listing_data)
    category_id = listing_data.get("category_id")
    if category_id in (None, ""):
        raise PlatformValidationError(
            "category_id is required for TikTok Shop publishing"
        )

    image_ids = (
        listing_data.get("tiktok_image_uri_list")
        or listing_data.get("image_uri_list")
        or []
    )
    if isinstance(image_ids, str):
        image_ids = [image_ids]
    image_ids = [str(item).strip() for item in image_ids if str(item).strip()]
    if not image_ids:
        raise PlatformValidationError(
            "tiktok_image_uri_list is required. Upload images to TikTok Shop first "
            "and pass the returned image URIs; public image URLs are not accepted here."
        )

    conversion_rate = float(listing_data.get("sgd_to_target_currency_rate", 1.0))
    currency_code = str(listing_data.get("currency_code") or "USD").upper()
    default_stock = max(1, int(listing_data.get("normal_stock", listing_data.get("stock", 10))))
    description = f"{listing_bullets_text(listing)}\n\n{listing.description}".strip()

    skus: list[dict[str, Any]] = []
    for variation in listing.variations:
        skus.append(
            {
                "seller_sku": variation.en_spec,
                "price": {
                    "amount": f"{round_money(variation.target_price_sgd * conversion_rate):.2f}",
                    "currency": currency_code,
                },
                "inventory": [{"quantity": default_stock}],
                "sales_attributes": [
                    {
                        "name": str(listing_data.get("variation_name") or "Style"),
                        "value": variation.en_spec,
                    }
                ],
            }
        )

    return {
        "title": listing.title,
        "description": description,
        "category_id": str(category_id),
        "status": str(listing_data.get("status") or "DRAFT").upper(),
        "main_images": [{"uri": image_id} for image_id in image_ids],
        "skus": skus,
        "package_weight": {
            "value": str(listing_data.get("weight", listing_data.get("package_weight", 0.1))),
            "unit": "KILOGRAM",
        },
        "product_attributes": listing_data.get("product_attributes") or [],
    }


class TikTokShopPublisher(PlatformPublisher):
    """TikTok Shop adapter implementing the common publisher interface."""

    platform = "tiktok_shop"

    def __init__(
        self,
        *,
        client: TikTokShopAPIClient | None = None,
        product_path: str = DEFAULT_PRODUCT_PATH,
    ) -> None:
        self.client = client or TikTokShopAPIClient.from_env()
        self.product_path = product_path

    def build_payload(self, listing_data: dict[str, Any]) -> dict[str, Any]:
        return build_tiktok_shop_product_payload(listing_data)

    def publish(self, listing_data: dict[str, Any]) -> PlatformPublishResult:
        payload = self.build_payload(listing_data)
        response = self.client.call("POST", self.product_path, body=payload)
        data = response.get("data") if isinstance(response.get("data"), dict) else response
        external_id = data.get("product_id") or data.get("id")
        return PlatformPublishResult(
            platform=self.platform,
            success=True,
            status=str(payload.get("status") or "DRAFT"),
            external_id=str(external_id) if external_id is not None else None,
            raw_response=response,
        )


def publish_tiktok_shop_product(
    listing_data: dict[str, Any],
    *,
    client: TikTokShopAPIClient | None = None,
) -> PlatformPublishResult:
    """Convenience helper for publishing one TikTok Shop product."""

    return TikTokShopPublisher(client=client).publish(listing_data)
CATEGORY_MAPPER = CategoryMapper()
