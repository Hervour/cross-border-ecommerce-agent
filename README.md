# 跨境代发系统

## 模块 1：货源数据采集与解析器

`source_scraper.py` 使用 `requests + BeautifulSoup4` 抓取并解析 1688 商品页，输出 Pydantic 模型 `RawProductData`。

```powershell
python source_scraper.py
```

支持以下数据来源：

- 真实 URL：使用浏览器 User-Agent、Referer、Sec-Fetch 等请求头，并配置重试策略。
- `mock://1688/camping-chair`：加载 `mock_data/mock_1688_product.html`。
- `mock://1688/camping-chair-json`：加载 `mock_data/mock_1688_product.json`。
- 网络失败、反爬页面、空测试文本或 `__MOCK_1688__`：自动回退到本地 Mock 数据。

解析器会尝试提取 HTML 选择器、JSON-LD、嵌入式 JSON、属性表格、主图列表和 SKU 列表。Mock 机制仅用于开发和测试，不用于绕过验证码或平台访问限制。

## 模块 2：智能商品转换与定价 Agent

把模块 1 采集到的中文货源数据转换为可交给 Shopee 刊登器使用的标准英文商品对象。核心大模型默认使用 DeepSeek，通过 OpenAI Python SDK 的兼容接口调用。

## 核心设计

- **DeepSeek 核心大脑**：默认模型为 `deepseek-flash`，默认兼容入口为 `https://api.deepseek.com`。
- **Pydantic 校验**：默认使用 DeepSeek JSON Object 模式，再通过 Pydantic `ListingDraft` 二次校验；支持 `OPENAI_STRUCTURED_MODE=json_schema` 的端点可切换为 Structured Outputs。
- **内容生成与定价分离**：LLM 负责翻译、卖点、Markdown 详情、类目和标签；Python 根据成本重新计算所有售价。
- **参数可覆盖**：构造 `ListingAgent` 时可以传入自定义 `model` 和 `base_url`。
- **最终模型可序列化**：`StandardProductListing.model_dump()` 的 `variations` 是普通 `dict` 列表，便于传给模块 3。

参考：[DeepSeek API Docs](https://api-docs.deepseek.com/)

## 目录

```text
ecommerce_agent/
  agent.py          # ListingAgent、DeepSeek 调用逻辑和系统提示词
  models.py         # Pydantic 数据模型
  pricing.py        # 定价公式与明细
demo_module2.py     # 中文露营椅测试数据与运行示例
tests/              # 无需调用真实 API 的单元测试
```

## 安装与运行

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

$env:OPENAI_API_KEY = "your-deepseek-api-key"
python demo_module2.py
```

也可以复制 `.env.example` 为 `.env`，然后填写：

```dotenv
OPENAI_API_KEY=your-deepseek-api-key
OPENAI_MODEL=deepseek-flash
OPENAI_BASE_URL=https://api.deepseek.com
```

`.env` 已被 `.gitignore` 忽略，不会被提交；不要把真实 Key 写入 `.env.example`。

默认配置等价于：

```python
agent = ListingAgent(
    model="deepseek-flash",
    base_url="https://api.deepseek.com",
)
```

自定义模型或兼容入口：

```powershell
$env:OPENAI_MODEL = "deepseek-v4-pro"
$env:OPENAI_BASE_URL = "https://your-compatible-endpoint.example/v1"
python demo_module2.py
```

也可以直接传参：

```python
agent = ListingAgent(
    model="your-model-name",
    base_url="https://your-compatible-endpoint.example/v1",
)
```

DeepSeek 当前默认使用 `response_format={"type":"json_object"}`，因为并非所有 DeepSeek 模型/端点都支持 OpenAI 的 JSON Schema Structured Outputs。

## 定价公式

净利率按“销售额”计算，而不是按采购成本加价：

```text
售价(SGD) = (采购成本(CNY) + 头程运费(CNY))
            × 汇率(CNY -> SGD)
            ÷ (1 - 平台佣金率 - 目标净利润率)
```

默认参数：

- 头程运费：CNY 10
- 平台佣金：10%
- 目标净利润率：35%
- 汇率：1 CNY = 0.19 SGD

最终售价向上取整到 SGD 0.01，保证取整不会吃掉利润。`calculate_pricing()` 会返回采购、运费、佣金、净利润和实际净利率明细，适合写入订单/刊登审计日志。

## 测试

```powershell
python -m unittest discover -s tests -v
```

测试使用假客户端验证 DeepSeek JSON Object 接线、Pydantic 校验、自定义端点和定价覆盖逻辑，不会消耗 API 额度。

## 模块 3：Shopee 商品自动化刊登器

`shopee_publisher.py` 支持两条刊登路径：

### 模式 A：Shopee Open API v2

使用 `partner_id + partner_key + timestamp + access_token + shop_id` 生成 HMAC-SHA256 签名，并调用：

```text
POST /api/v2/product/add_item
```

商品默认使用 `item_status=UNLIST`，相当于未上架商品。API 模式需要：

```powershell
$env:SHOPEE_PARTNER_ID = "123456"
$env:SHOPEE_PARTNER_KEY = "partner-key"
$env:SHOPEE_ACCESS_TOKEN = "access-token"
$env:SHOPEE_SHOP_ID = "789012"
```

调用示例：

```python
from shopee_publisher import upload_as_draft

api_data = listing.model_dump()
api_data.update({
    "category_id": 100017,
    "image_id_list": ["shopee-image-id"],
    "weight": 1.2,
    "normal_stock": 10,
    "logistic_info": [{"logistic_id": 1, "enabled": True}],
    "attribute_list": [],
})
result = upload_as_draft(api_data)
```

`image_id_list` 必须是通过 Shopee 媒体接口上传后得到的图片 ID，不是公开图片 URL。`category_id` 也必须是数字类目 ID。

### 模式 B：批量上传 Excel

```python
from shopee_publisher import export_shopee_mass_upload_excel

export_shopee_mass_upload_excel(listing, "shopee_mass_upload.xlsx")
```

输出包含 `Mass Upload` 和 `Instructions` 工作表。Shopee 各站点、各一级类目下载的批量模板列名可能不同，正式上传时应以 Seller Centre 下载的类目模板为准，再映射本模块生成的字段。

运行演示：

```powershell
python shopee_publisher.py
```

演示会生成 `shopee_mass_upload_demo.xlsx`。如果 Shopee API 环境变量齐全，还会继续调用 `add_item`；否则只执行 Excel 导出。

## 模块 4：订单监听与一键代采通知器

`order_fulfillment.py` 使用 FastAPI 提供订单 Webhook，并完成 SKU 映射、利润校验、人工代采通知和物流单号回传。

启动服务：

```powershell
$env:ORDER_MAPPING_PATH = "C:\Users\LHW66\Documents\ChatGPT\New project 3\data\sku_mapping.json"
python -m uvicorn order_fulfillment:app --host 0.0.0.0 --port 8000
```

也可以运行项目根目录的一键启动脚本：

```powershell
.\run_order_service.ps1
```

启动后打开：

```text
http://127.0.0.1:8000/
```

Webhook：

```text
POST /api/v1/shopee/order_webhook
```

支持接收 `ordersn/order_sn`、`item_list/items`、`recipient_address`、`paid_amount` 等字段。真实 Shopee 创建订单推送如果只包含订单号，需要先调用 `v2.order.get_order_detail` 补全商品和地址，再转发给该接口。

利润公式：

```text
预计利润 = 买家实付金额 × 换算汇率 - Σ(1688 采购成本 + 运费) × 数量
```

负利润或缺少 SKU 映射时，状态分别标记为 `ANOMALY_LOW_PROFIT` 或 `ANOMALY_MAPPING_MISSING`，并触发通知。

通知渠道通过环境变量配置：

```powershell
$env:WECOM_WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."
$env:FEISHU_WEBHOOK_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/..."
$env:TELEGRAM_BOT_TOKEN = "..."
$env:TELEGRAM_CHAT_ID = "..."
$env:PURCHASE_CONFIRM_BASE_URL = "http://localhost:8000/api/v1/purchase/confirm"
$env:PURCHASE_CONFIRM_SECRET = "change-me"
```

通知包含 Shopee 订单号、预计利润、1688 采购链接、买家地址以及人工确认按钮/链接。

物流单号回传：

```python
from order_fulfillment import sync_tracking_number

sync_tracking_number("SHOPEE-ORDER-001", "SF123456789")
```

底层调用 Shopee Open API v2：

```text
POST /api/v2/logistics/ship_order
```

采购发货后可通过服务接口同步：

```text
POST /api/v1/shopee/tracking/{shopee_order_id}?tracking_code=SF123456789
```

### 多平台刊登适配器

`platform_adapters/` 提供统一接口：

```python
from platform_adapters import get_publisher

publisher = get_publisher("aliexpress")
result = publisher.publish(listing_data)
```

当前支持：

- `shopee`
- `aliexpress`
- `tiktok_shop`

AliExpress 使用 `app_secret + 排序参数 + app_secret` 的 MD5 大写签名，需要：

```powershell
$env:ALIEXPRESS_APP_KEY = "..."
$env:ALIEXPRESS_APP_SECRET = "..."
$env:ALIEXPRESS_ACCESS_TOKEN = "..."
```

TikTok Shop 使用 HMAC-SHA256，签名内容为：

```text
app_secret + path + 排序参数 + JSON body + app_secret
```

需要：

```powershell
$env:TIKTOK_SHOP_APP_KEY = "..."
$env:TIKTOK_SHOP_APP_SECRET = "..."
$env:TIKTOK_SHOP_ACCESS_TOKEN = "..."
$env:TIKTOK_SHOP_CIPHER = "..."
```

AliExpress 商品映射需要 `category_id` 和公开 `image_urls`。TikTok Shop 商品映射需要 `category_id` 和上传后得到的 `tiktok_image_uri_list`。TikTok 商品默认以 `DRAFT` 状态提交。

订单适配器支持：

- `POST /api/v1/aliexpress/order_webhook`
- `POST /api/v1/tiktok/order_webhook`
- `POST /api/v1/{platform}/tracking/{order_id}?tracking_code=...`

其中 `platform` 可传入 `aliexpress` 或 `tiktok_shop`。AliExpress 订单解析兼容 `order_id/trade_order_id`、`product_list/items`、`receipt_address/shipping_address`；TikTok Shop 订单解析兼容 `order_id`、`line_items/items`、`recipient_address` 和 `payment` 金额结构。

## 生产化增强

### SQLite 持久化与幂等

订单服务默认使用 `data/fulfillment.db` 保存：

- SKU 货源映射。
- Webhook 事件，防止重复订单触发多次通知。
- 订单利润和决策。
- 人工确认状态。
- 物流单号回传记录。

数据库路径可通过 `ORDER_DB_PATH` 修改。

### 订单管理页

服务运行后访问：

```text
http://127.0.0.1:8000/orders
```

可查看平台、订单号、利润状态、利润金额、确认状态和更新时间。

### 类目映射

`category_mapper.py` 和 `data/category_mapping.json` 支持根据商品类目或标题自动补充 AliExpress 和 TikTok Shop 的 `category_id` 及基础属性。后续可以继续扩展更多类目。

### Docker 部署

```powershell
docker compose up -d --build
```

`docker-compose.yml` 会把 `data/` 挂载到容器外，避免 SQLite 数据随容器删除。生产环境还需要把 `8000` 端口放在 HTTPS 反向代理、Cloudflare Tunnel 或 VPS 网关之后。