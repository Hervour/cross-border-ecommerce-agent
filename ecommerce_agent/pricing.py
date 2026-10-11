"""Deterministic pricing rules for cross-border dropshipping."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP


@dataclass(frozen=True, slots=True)
class PricingBreakdown:
    """Detailed pricing result used for logs, audits, and merchant approval."""

    cost_cny: float
    estimated_shipping_cny: float
    total_cost_cny: float
    total_cost_sgd: float
    target_profit_margin: float
    platform_commission_rate: float
    raw_selling_price_sgd: float
    target_price_sgd: float
    platform_commission_sgd: float
    estimated_net_profit_sgd: float
    actual_net_margin: float


def _as_decimal(value: float, field_name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    if not math.isfinite(float(value)):
        raise ValueError(f"{field_name} must be finite")
    return Decimal(str(value))


def _validate_rate(value: float, field_name: str) -> Decimal:
    rate = _as_decimal(value, field_name)
    if rate < 0 or rate >= 1:
        raise ValueError(f"{field_name} must be between 0 (inclusive) and 1 (exclusive)")
    return rate


def calculate_pricing(
    cost_cny: float,
    profit_margin: float = 0.35,
    exchange_rate_cny_to_sgd: float = 0.19,
    *,
    estimated_shipping_cny: float = 10.0,
    platform_commission_rate: float = 0.10,
) -> PricingBreakdown:
    """Calculate a price that preserves the requested net margin.

    Formula:
        price = (procurement cost + first-leg shipping) * exchange rate
                / (1 - platform commission rate - target net margin)

    Net margin is measured against revenue, not against cost. The final price is
    rounded up to the nearest SGD 0.01 so that rounding cannot reduce the margin.
    """

    cost = _as_decimal(cost_cny, "cost_cny")
    shipping = _as_decimal(estimated_shipping_cny, "estimated_shipping_cny")
    exchange_rate = _as_decimal(exchange_rate_cny_to_sgd, "exchange_rate_cny_to_sgd")
    margin = _validate_rate(profit_margin, "profit_margin")
    commission_rate = _validate_rate(platform_commission_rate, "platform_commission_rate")

    if cost < 0:
        raise ValueError("cost_cny must be greater than or equal to 0")
    if shipping < 0:
        raise ValueError("estimated_shipping_cny must be greater than or equal to 0")
    if exchange_rate <= 0:
        raise ValueError("exchange_rate_cny_to_sgd must be greater than 0")

    denominator = Decimal("1") - commission_rate - margin
    if denominator <= 0:
        raise ValueError(
            "profit_margin + platform_commission_rate must be less than 1"
        )

    total_cost_cny = cost + shipping
    total_cost_sgd = total_cost_cny * exchange_rate
    raw_price = total_cost_sgd / denominator
    cent = Decimal("0.01")
    target_price = raw_price.quantize(cent, rounding=ROUND_CEILING)

    commission = (target_price * commission_rate).quantize(
        cent, rounding=ROUND_HALF_UP
    )
    net_profit = target_price - total_cost_sgd - commission
    actual_net_margin = net_profit / target_price if target_price else Decimal("0")

    return PricingBreakdown(
        cost_cny=float(cost),
        estimated_shipping_cny=float(shipping),
        total_cost_cny=float(total_cost_cny),
        total_cost_sgd=float(total_cost_sgd),
        target_profit_margin=float(margin),
        platform_commission_rate=float(commission_rate),
        raw_selling_price_sgd=float(raw_price),
        target_price_sgd=float(target_price),
        platform_commission_sgd=float(commission),
        estimated_net_profit_sgd=float(net_profit),
        actual_net_margin=float(actual_net_margin),
    )


def calculate_selling_price(
    cost_cny: float,
    profit_margin: float = 0.35,
    exchange_rate_cny_to_sgd: float = 0.19,
    *,
    estimated_shipping_cny: float = 10.0,
    platform_commission_rate: float = 0.10,
) -> float:
    """Return only the rounded target selling price in SGD."""

    return calculate_pricing(
        cost_cny=cost_cny,
        profit_margin=profit_margin,
        exchange_rate_cny_to_sgd=exchange_rate_cny_to_sgd,
        estimated_shipping_cny=estimated_shipping_cny,
        platform_commission_rate=platform_commission_rate,
    ).target_price_sgd