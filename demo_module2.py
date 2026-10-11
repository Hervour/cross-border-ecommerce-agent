"""Runnable module-2 demo using a raw Chinese camping-chair record."""

from __future__ import annotations

import os
import sys
from typing import Any

from ecommerce_agent import ListingAgent, calculate_pricing

RAW_CAMPING_CHAIR_DATA: dict[str, Any] = {
    "source_platform": "1688",
    "source_product_id": "A1688-CAMP-CHAIR-001",
    "supplier_title": "户外便携折叠露营椅 铝合金月亮椅 钓鱼椅 承重150kg",
    "raw_description": (
        "工厂直供，采用铝合金支架和600D加厚牛津布，展开尺寸约52x46x68cm，"
        "收纳尺寸约35x12x12cm，净重约1.1kg。适合露营、钓鱼、野餐和阳台休闲。"
        "本店另有大量批发款式，请联系客服获取工厂批发报价。"
    ),
    "attributes": {
        "框架材质": "铝合金",
        "面料": "600D牛津布",
        "展开尺寸": "52 x 46 x 68 cm",
        "收纳尺寸": "35 x 12 x 12 cm",
        "净重": "约1.1 kg",
        "承重": "150 kg",
    },
    "selling_points": [
        "折叠后体积小，方便放入汽车后备箱",
        "铝合金支架，轻量且便于携带",
        "600D牛津布坐面，耐磨易清洁",
        "侧边网袋可放水瓶和手机",
        "适合露营、钓鱼、野餐和户外音乐会",
    ],
    "skus": [
        {
            "原规格": "米白色 标准款 铝合金支架",
            "采购价_cny": 58.0,
            "库存": 320,
        },
        {
            "原规格": "黑色 标准款 铝合金支架",
            "采购价_cny": 58.0,
            "库存": 280,
        },
        {
            "原规格": "军绿色 加宽款 带侧边网袋",
            "采购价_cny": 69.0,
            "库存": 160,
        },
    ],
    "supplier_noise": "联系客服拿工厂批发价，非质量问题退换请先沟通",
}


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit(
            "OPENAI_API_KEY is not set. PowerShell example:\n"
            "$env:OPENAI_API_KEY='your-api-key'\n"
            "Then run: python demo_module2.py"
        )

    agent = ListingAgent()
    listing = agent.transform(RAW_CAMPING_CHAIR_DATA)

    print("\n=== Standard English Listing ===")
    print(listing.model_dump_json(indent=2, ensure_ascii=False))

    print("\n=== Pricing Breakdown (SGD) ===")
    for variation in listing.variations:
        pricing = calculate_pricing(
            cost_cny=variation.cost_cny,
            profit_margin=agent.profit_margin,
            exchange_rate_cny_to_sgd=agent.exchange_rate_cny_to_sgd,
            estimated_shipping_cny=agent.estimated_shipping_cny,
            platform_commission_rate=agent.platform_commission_rate,
        )
        print(
            f"- {variation.en_spec}\n"
            f"  Supplier cost: CNY {pricing.cost_cny:.2f}\n"
            f"  First-leg shipping: CNY {pricing.estimated_shipping_cny:.2f}\n"
            f"  Landed cost before platform fee: SGD {pricing.total_cost_sgd:.2f}\n"
            f"  Platform commission: SGD {pricing.platform_commission_sgd:.2f}\n"
            f"  Target price: SGD {pricing.target_price_sgd:.2f}\n"
            f"  Estimated net profit: SGD {pricing.estimated_net_profit_sgd:.2f} "
            f"({pricing.actual_net_margin:.1%} net margin)"
        )


if __name__ == "__main__":
    main()