"""Module 1: fetch and parse raw 1688 product data.

The parser supports classic HTML selectors, JSON-LD, embedded JSON payloads,
attribute tables, image galleries, and SKU tables. Network failures fall back
to the local mock files in ``mock_data/`` so downstream development can continue
without bypassing a marketplace's anti-bot controls.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, ConfigDict, Field, field_validator
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOGGER = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
MOCK_DATA_DIR = BASE_DIR / "mock_data"
MOCK_HTML_PATH = MOCK_DATA_DIR / "mock_1688_product.html"
MOCK_JSON_PATH = MOCK_DATA_DIR / "mock_1688_product.json"

MOCK_TRIGGERS = {"mock", "test", "test_text", "__mock__", "__mock_1688__"}
PRICE_KEYS = {
    "price",
    "price_cny",
    "priceCny",
    "lowPrice",
    "highPrice",
    "minPrice",
    "maxPrice",
    "wholesalePrice",
    "purchasePrice",
}
SKU_LIST_KEYS = ("skus", "skuList", "skuInfos", "skuInfoList", "variants", "hasVariant")
ATTRIBUTE_KEYS = (
    "attributes",
    "additionalProperty",
    "properties",
    "specifications",
    "productProperties",
)
IMAGE_KEYS = (
    "image",
    "images",
    "imageUrl",
    "image_url",
    "image_urls",
    "mainImage",
    "mainImages",
    "picUrl",
    "pic_url",
    "thumbnails",
)
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


class RawProductData(BaseModel):
    """Normalized module-1 output consumed by the listing agent."""

    model_config = ConfigDict(extra="forbid")

    source_url: str = Field(min_length=1)
    original_title: str = Field(min_length=1)
    price_range_cny: str = "N/A"
    attributes: dict[str, str] = Field(default_factory=dict)
    image_urls: list[str] = Field(default_factory=list)
    skus: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("attributes", mode="before")
    @classmethod
    def normalize_attributes(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, Mapping):
            return {}
        normalized: dict[str, str] = {}
        for key, item in value.items():
            clean_key = _clean_text(str(key))
            if isinstance(item, (Mapping, list, tuple, set)):
                continue
            clean_value = _clean_text(str(item))
            if clean_key and clean_value:
                normalized[clean_key] = clean_value
        return normalized


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def load_mock_html(path: str | Path | None = None) -> str:
    """Load the local 1688 HTML fixture."""

    mock_path = Path(path) if path else MOCK_HTML_PATH
    return mock_path.read_text(encoding="utf-8")


def load_mock_json_text(path: str | Path | None = None) -> str:
    """Load the local JSON fixture as text."""

    mock_path = Path(path) if path else MOCK_JSON_PATH
    return mock_path.read_text(encoding="utf-8")


def load_mock_json(path: str | Path | None = None) -> dict[str, Any]:
    """Load and decode the local JSON fixture."""

    payload = json.loads(load_mock_json_text(path))
    if not isinstance(payload, dict):
        raise ValueError("The local mock JSON must contain an object")
    return payload


def _build_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _looks_like_antibot_page(html: str) -> bool:
    lowered = html.lower()
    if len(html) > 20_000:
        return False
    indicators = (
        "captcha",
        "x5secdata",
        "punish",
        "验证码",
        "滑动验证",
        "访问受限",
        "登录后继续",
        "security check",
    )
    return any(indicator in lowered for indicator in indicators)


def fetch_product_by_url(url: str) -> str:
    """Fetch a product page as HTML.

    ``mock://`` URLs and network failures return a local fixture. This is a
    development fallback, not a CAPTCHA bypass or anti-bot circumvention.
    """

    normalized_url = _clean_text(url)
    if not normalized_url:
        LOGGER.warning("Empty URL received; loading local mock HTML")
        return load_mock_html()

    if normalized_url.startswith("mock://"):
        return load_mock_json_text() if "json" in normalized_url.lower() else load_mock_html()

    parsed = urlparse(normalized_url)
    headers: dict[str, str] = {}
    if parsed.scheme and parsed.netloc:
        headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"

    session = _build_session()
    try:
        response = session.get(
            normalized_url,
            headers=headers,
            timeout=(10, 30),
            allow_redirects=True,
        )
        response.raise_for_status()

        if not response.encoding or response.encoding.lower() == "iso-8859-1":
            response.encoding = response.apparent_encoding or "utf-8"

        html = response.text
        if _looks_like_antibot_page(html):
            LOGGER.warning("Anti-bot page detected; loading local mock HTML")
            return load_mock_html()
        return html
    except requests.RequestException as exc:
        LOGGER.warning("Request failed (%s); loading local mock HTML", exc)
        return load_mock_html()
    finally:
        session.close()

def parse_1688_html(html_content: str) -> RawProductData:
    """Parse a 1688 product page or a local mock HTML/JSON string."""

    return _parse_1688_content(html_content, allow_mock_fallback=True)


def _parse_1688_content(
    html_content: str, *, allow_mock_fallback: bool
) -> RawProductData:
    if not isinstance(html_content, str):
        raise TypeError("html_content must be a string")

    text = html_content.strip()
    if not text or text.lower() in MOCK_TRIGGERS or ("<" not in text and "{" not in text):
        if not allow_mock_fallback:
            raise ValueError("Mock document could not be parsed")
        LOGGER.warning("Test text or empty HTML received; loading local mock HTML")
        text = load_mock_html()

    payload_roots: list[Any] = []
    soup: BeautifulSoup | None = None

    if text.startswith("{") or text.startswith("["):
        try:
            payload_roots = [json.loads(text)]
        except json.JSONDecodeError as exc:
            LOGGER.warning("JSON parsing failed (%s); falling back to HTML parser", exc)

    if not payload_roots:
        soup = BeautifulSoup(text, "html.parser")
        payload_roots = _extract_json_payloads(soup)

    payloads: list[Any] = list(_walk_json(payload_roots))

    source_url = _extract_source_url(soup, payloads)
    title = _extract_title(soup, payloads)
    price_range = _extract_price_range(soup, payloads)
    attributes = _extract_attributes(soup, payloads)
    image_urls = _extract_images(soup, payloads, source_url)
    skus = _extract_skus(soup, payloads)

    is_empty = (
        not title
        and price_range == "N/A"
        and not attributes
        and not image_urls
        and not skus
    )
    if is_empty and allow_mock_fallback:
        LOGGER.warning("No product fields found; loading local mock HTML")
        return _parse_1688_content(load_mock_html(), allow_mock_fallback=False)

    if not title and soup is not None and soup.title:
        title = _clean_title(soup.title.get_text(" ", strip=True))

    return RawProductData(
        source_url=source_url or "unknown://1688/product",
        original_title=title or "Unknown product",
        price_range_cny=price_range,
        attributes=attributes,
        image_urls=image_urls,
        skus=skus,
    )


def _extract_json_payloads(soup: BeautifulSoup) -> list[Any]:
    payloads: list[Any] = []
    for script in soup.find_all("script"):
        raw = script.string or script.get_text("", strip=False)
        raw = raw.strip()
        if not raw or len(raw) > 2_000_000:
            continue
        candidate = raw
        if not (candidate.startswith("{") or candidate.startswith("[")):
            match = re.search(r"(?:=|:)\s*([\[{].*[\]}])\s*;?\s*$", candidate, flags=re.S)
            if not match:
                continue
            candidate = match.group(1)
        try:
            payloads.append(json.loads(candidate))
        except json.JSONDecodeError:
            continue
    return payloads


def _walk_json(value: Any) -> Iterator[Any]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _first_text_value(payloads: Iterable[Any], keys: Iterable[str]) -> str:
    key_order = tuple(keys)
    for key in key_order:
        for node in payloads:
            if not isinstance(node, dict):
                continue
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return _clean_text(value)
    return ""


def _meta_content(soup: BeautifulSoup, *, property_name: str) -> str:
    tag = soup.find("meta", attrs={"property": property_name})
    if not tag:
        tag = soup.find("meta", attrs={"name": property_name})
    content = tag.get("content") if tag else None
    return _clean_text(str(content)) if content else ""


def _clean_title(value: str) -> str:
    title = _clean_text(value)
    title = re.sub(r"\s*[-|｜]\s*(?:1688|阿里巴巴|Alibaba).*$", "", title, flags=re.I)
    return title.strip()


def _extract_title(soup: BeautifulSoup | None, payloads: list[Any]) -> str:
    payload_title = _first_text_value(
        payloads, ("original_title", "productTitle", "title", "name", "subject")
    )
    if payload_title:
        return _clean_title(payload_title)

    if soup is None:
        return ""

    for key in ("og:title", "twitter:title"):
        value = _meta_content(soup, property_name=key)
        if value:
            return _clean_title(value)

    selectors = (
        "h1.offer-title",
        "h1.detail-title",
        ".title-text",
        ".offer-title",
        ".product-title h1",
        "main h1",
        "h1",
    )
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            value = _clean_title(node.get_text(" ", strip=True))
            if value:
                return value
    return ""


def _numbers_in_text(value: str) -> list[float]:
    numbers: list[float] = []
    for match in re.findall(r"(?<!\d)(\d{1,7}(?:\.\d{1,4})?)(?!\d)", value):
        try:
            number = float(match)
        except ValueError:
            continue
        if 0 < number <= 1_000_000:
            numbers.append(number)
    return numbers


def _format_price_range(numbers: Iterable[float]) -> str:
    unique = sorted({round(float(number), 2) for number in numbers if float(number) > 0})
    if not unique:
        return "N/A"
    if len(unique) == 1:
        return f"{unique[0]:.2f}"
    return f"{unique[0]:.2f}-{unique[-1]:.2f}"


def _extract_price_range(soup: BeautifulSoup | None, payloads: list[Any]) -> str:
    for node in payloads:
        if not isinstance(node, dict):
            continue
        for key in ("price_range_cny", "priceRange", "price_range"):
            value = node.get(key)
            if isinstance(value, str):
                numbers = _numbers_in_text(value)
                if numbers:
                    return _format_price_range(numbers)

    price_values: list[float] = []
    for node in payloads:
        if not isinstance(node, dict):
            continue
        for key in PRICE_KEYS:
            value = node.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if 0 < float(value) <= 1_000_000:
                    price_values.append(float(value))
            elif isinstance(value, str):
                numbers = _numbers_in_text(value)
                if numbers:
                    price_values.extend(numbers)

    if soup is not None:
        for key in ("product:price:amount", "og:price:amount", "price"):
            value = _meta_content(soup, property_name=key)
            if value:
                price_values.extend(_numbers_in_text(value))

        selectors = (
            ".price-range",
            ".price-text",
            ".currency-value",
            ".price",
            "[data-testid='price']",
        )
        for selector in selectors:
            for node in soup.select(selector)[:20]:
                price_values.extend(_numbers_in_text(node.get_text(" ", strip=True)))

    return _format_price_range(price_values)

def _add_attribute(attributes: dict[str, str], key: Any, value: Any) -> None:
    if isinstance(value, (Mapping, list, tuple, set)):
        return
    clean_key = _clean_text(str(key))
    clean_value = _clean_text(str(value))
    if not clean_key or not clean_value or len(clean_key) > 80 or len(clean_value) > 500:
        return
    if clean_key.lower() in {"price", "价格", "sku", "库存"}:
        return
    attributes.setdefault(clean_key, clean_value)


def _extract_attributes(
    soup: BeautifulSoup | None, payloads: list[Any]
) -> dict[str, str]:
    attributes: dict[str, str] = {}

    for node in payloads:
        if not isinstance(node, dict):
            continue
        for key in ATTRIBUTE_KEYS:
            value = node.get(key)
            if isinstance(value, dict):
                for attr_key, attr_value in value.items():
                    _add_attribute(attributes, attr_key, attr_value)
            elif isinstance(value, list):
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    attr_key = (
                        item.get("name")
                        or item.get("key")
                        or item.get("propertyName")
                        or item.get("label")
                    )
                    attr_value = item.get("value") or item.get("propertyValue")
                    if attr_key:
                        _add_attribute(attributes, attr_key, attr_value)

    if soup is None:
        return attributes

    for row in soup.select("table.attributes-table tr, table.property-table tr, dl"):
        if row.name == "dl":
            terms = row.find_all("dt", recursive=False)
            definitions = row.find_all("dd", recursive=False)
            for term, definition in zip(terms, definitions):
                _add_attribute(
                    attributes,
                    term.get_text(" ", strip=True),
                    definition.get_text(" ", strip=True),
                )
            continue

        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) < 2:
            continue
        label = cells[0].get_text(" ", strip=True)
        value = " / ".join(cell.get_text(" ", strip=True) for cell in cells[1:])
        _add_attribute(attributes, label, value)

    for node in soup.select(".attribute-item, .property-item, .product-property li")[:80]:
        text = node.get_text(" ", strip=True)
        parts = re.split(r"[:：]", text, maxsplit=1)
        if len(parts) == 2:
            _add_attribute(attributes, parts[0], parts[1])

    return dict(list(attributes.items())[:100])


def _normalize_image_url(value: Any, base_url: str) -> str:
    if not isinstance(value, str):
        return ""
    image_url = _clean_text(value).replace("\\/", "/")
    if not image_url or image_url.startswith("data:"):
        return ""
    if image_url.startswith("//"):
        image_url = f"https:{image_url}"
    elif image_url.startswith("/"):
        usable_base = base_url if base_url.startswith(("http://", "https://")) else "https://detail.1688.com"
        image_url = urljoin(usable_base, image_url)
    elif not image_url.startswith(("http://", "https://")):
        return ""

    lowered = image_url.lower()
    if any(
        token in lowered
        for token in ("sprite", "logo", "avatar", "icon", "loading", "blank.gif")
    ):
        return ""
    return image_url


def _collect_image_values(value: Any, target: list[Any]) -> None:
    if isinstance(value, str):
        target.append(value)
    elif isinstance(value, list):
        for item in value:
            _collect_image_values(item, target)
    elif isinstance(value, dict):
        for key in ("url", "src", "imageUrl", "image_url"):
            if key in value:
                _collect_image_values(value[key], target)


def _extract_images(
    soup: BeautifulSoup | None, payloads: list[Any], source_url: str
) -> list[str]:
    raw_urls: list[Any] = []
    for node in payloads:
        if not isinstance(node, dict):
            continue
        if any(node.get(key) not in (None, "") for key in ("sku_id", "skuId", "original_spec")):
            continue
        for key in IMAGE_KEYS:
            if key in node:
                _collect_image_values(node[key], raw_urls)

    if soup is not None:
        for key in ("og:image", "twitter:image", "twitter:image:src"):
            value = _meta_content(soup, property_name=key)
            if value:
                raw_urls.append(value)

        selectors = (
            ".detail-gallery img",
            ".image-list img",
            ".main-image img",
            "[class*='gallery'] img",
            "[class*='thumbnail'] img",
        )
        for selector in selectors:
            for image in soup.select(selector):
                for attr in ("src", "data-src", "data-lazy-src", "data-original"):
                    if image.get(attr):
                        raw_urls.append(image.get(attr))

        for image in soup.find_all("img"):
            context = " ".join(
                [
                    " ".join(image.get("class", [])),
                    str(image.get("id", "")),
                    " ".join(image.parent.get("class", [])) if isinstance(image.parent, Tag) else "",
                    str(image.get("alt", "")),
                ]
            ).lower()
            if not any(
                token in context
                for token in ("main", "gallery", "thumb", "product", "pic", "图片", "主图")
            ):
                continue
            for attr in ("src", "data-src", "data-lazy-src", "data-original"):
                if image.get(attr):
                    raw_urls.append(image.get(attr))

    normalized = [
        _normalize_image_url(value, source_url)
        for value in raw_urls
    ]
    return list(dict.fromkeys(url for url in normalized if url))

def _stringify_spec(value: Any) -> str:
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                part = item.get("value") or item.get("name") or item.get("propertyValue")
                if part:
                    parts.append(_clean_text(str(part)))
            elif isinstance(item, (str, int, float)):
                parts.append(_clean_text(str(item)))
        return " / ".join(dict.fromkeys(parts))
    if isinstance(value, dict):
        for key in ("value", "name", "text", "label"):
            if value.get(key):
                return _clean_text(str(value[key]))
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _first_number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value), 2)
    if isinstance(value, str):
        numbers = _numbers_in_text(value)
        return numbers[0] if numbers else None
    if isinstance(value, dict):
        for key in ("price", "value", "amount"):
            if key in value:
                return _first_number(value[key])
    return None


def _normalize_sku(item: Any, fallback_spec: str = "") -> dict[str, Any] | None:
    if not isinstance(item, dict):
        spec = _stringify_spec(item) or fallback_spec
        return {"original_spec": spec} if spec else None

    spec = ""
    for key in (
        "original_spec",
        "spec",
        "specText",
        "skuName",
        "propertiesName",
        "properties_name",
        "name",
        "properties",
        "color",
        "size",
    ):
        if key in item:
            spec = _stringify_spec(item[key])
            if spec:
                break
    spec = spec or fallback_spec
    if not spec:
        return None

    normalized: dict[str, Any] = {"original_spec": spec}
    id_value = item.get("sku_id") or item.get("skuId") or item.get("id")
    if id_value not in (None, ""):
        normalized["sku_id"] = str(id_value)

    price_value = None
    for key in ("cost_cny", "costCny", "price_cny", "purchasePrice", "cost", "price"):
        if key in item:
            price_value = _first_number(item[key])
            if price_value is not None:
                break
    if price_value is not None:
        normalized["cost_cny"] = price_value

    stock_value = item.get("stock") or item.get("quantity") or item.get("amount")
    if stock_value not in (None, ""):
        try:
            normalized["stock"] = int(float(str(stock_value)))
        except (TypeError, ValueError):
            normalized["stock"] = str(stock_value)

    image_value = item.get("image_url") or item.get("imageUrl") or item.get("image")
    if image_value:
        image_text = _clean_text(str(image_value)).replace("\\/", "/")
        if image_text.startswith("//"):
            image_text = f"https:{image_text}"
        normalized["image_url"] = image_text

    for key, value in item.items():
        if key in normalized or key in PRICE_KEYS or key in ATTRIBUTE_KEYS:
            continue
        if isinstance(value, (str, int, float, bool)) and key not in normalized:
            normalized[key] = value

    return normalized


def _dedupe_skus(skus: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sku in skus:
        marker = json.dumps(sku, ensure_ascii=False, sort_keys=True, default=str)
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(sku)
    return unique


def _extract_skus(soup: BeautifulSoup | None, payloads: list[Any]) -> list[dict[str, Any]]:
    skus: list[dict[str, Any]] = []

    for node in payloads:
        if not isinstance(node, dict):
            continue

        for key in SKU_LIST_KEYS:
            value = node.get(key)
            if isinstance(value, list):
                for item in value:
                    normalized = _normalize_sku(item)
                    if normalized:
                        skus.append(normalized)
            elif isinstance(value, dict):
                normalized = _normalize_sku(value)
                if normalized:
                    skus.append(normalized)

        for key in ("skuMap", "skuInfoMap", "sku_map"):
            value = node.get(key)
            if not isinstance(value, dict):
                continue
            for spec, item in value.items():
                if isinstance(item, dict):
                    normalized = _normalize_sku(item, fallback_spec=str(spec))
                    if normalized:
                        skus.append(normalized)
                else:
                    skus.append({"original_spec": _clean_text(str(spec))})

    if skus or soup is None:
        return _dedupe_skus(skus)

    for table in soup.find_all("table"):
        header_text = _clean_text(table.get_text(" ", strip=True))
        table_hint = " ".join(table.get("class", [])) + " " + str(table.get("id", ""))
        if not any(token in header_text + table_hint for token in ("规格", "颜色", "sku", "SKU", "价格")):
            continue

        rows = table.find_all("tr")
        if not rows:
            continue
        headers = [_clean_text(cell.get_text(" ", strip=True)) for cell in rows[0].find_all(["th", "td"])]
        for row in rows[1:]:
            cells = row.find_all(["th", "td"])
            values = [_clean_text(cell.get_text(" ", strip=True)) for cell in cells]
            if len(values) < 2:
                continue
            item = dict(zip(headers, values)) if headers else {}
            if "original_spec" not in item:
                item["original_spec"] = values[0]
            normalized = _normalize_sku(item)
            if normalized:
                skus.append(normalized)

    return _dedupe_skus(skus)


def _extract_source_url(soup: BeautifulSoup | None, payloads: list[Any]) -> str:
    if soup is not None:
        canonical = soup.find("link", attrs={"rel": lambda value: value and "canonical" in value})
        if canonical and canonical.get("href"):
            return _clean_text(str(canonical.get("href")))
        for key in ("og:url", "twitter:url"):
            value = _meta_content(soup, property_name=key)
            if value:
                return value

    return _first_text_value(
        payloads, ("source_url", "sourceUrl", "canonical_url", "url")
    )


def _print_product(product: RawProductData) -> None:
    print("=== RawProductData ===")
    print(json.dumps(product.model_dump(), indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    mock_url = "mock://1688/camping-chair"
    html = fetch_product_by_url(mock_url)
    product_data = parse_1688_html(html)
    _print_product(product_data)