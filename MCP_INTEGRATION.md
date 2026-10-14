# MCP 集成说明

本文说明本项目**实际使用**的麦当劳 MCP Server、Tool、调用流程与业务价值，并如实标注
哪些是官方文档确认的事实、哪些是本项目的工程假设。

---

## 1. 使用的 MCP Server

| 项目 | 值 |
|---|---|
| 接入地址 | `https://mcp.mcd.cn`（写死，不接受用户自定义） |
| 协议 | **Streamable HTTP** |
| 鉴权 | 请求头 `Authorization: Bearer YOUR_MCP_TOKEN` |
| Token 申领 | <https://open.mcd.cn/mcp>（手机号登录 → 控制台 → 激活 → 复制） |
| 限流 | 每个 Token **每分钟最多 600 次**请求，超限返回 **429** |
| 错误码 | `401` Token 无效/过期/未提供；`429` 触发限流 |

配置示例见仓库根目录的 [`mcp-config.example.json`](mcp-config.example.json)：

```json
{
  "mcpServers": {
    "mcd-mcp": {
      "type": "streamablehttp",
      "url": "https://mcp.mcd.cn",
      "headers": { "Authorization": "Bearer ${MCD_MCP_TOKEN}" }
    }
  }
}
```

> 仓库内**不含任何**真实 Token。Token 只从环境变量 `MCD_MCP_TOKEN` 读取。

---

## 2. 使用的 Tool

本项目**只读**，只用下面 4 个工具。其余 33 个官方工具本项目一概不调用。

| Tool | 读/写 | 在本项目中的用途 | 参与结论推导？ |
|---|---|---|---|
| `order-list` | 只读 | **核心数据源**：拉取最近 10 单历史订单，是一切结论的唯一来源 | ✅ 是 |
| `now-time-info` | 只读 | 取服务端当前时间，用于校对本地时钟偏移（避免把「今天」算错而误判倦怠） | ❌ 否，仅做上下文校对 |
| `query-my-account` | 只读 | 确认调用方身份可用（不把任何账户字段写进报告或长图） | ❌ 否 |
| `query-my-coupons` | 只读 | 作为可解释背景（例如「窗口内有券可用」），本项目**不做任何券推断** | ❌ 否 |

### 明确不使用的写操作

下列官方工具会改动用户账户，本项目**绝不调用**（也没有实现任何调用路径）：

`create-order`、`cancel-order`、`auto-bind-coupons`、`party-order-create`、
`mall-create-order`、`draw-lottery`、`delivery-create-address`

也就是说：**本项目不会下单、不会取消订单、不会领券、不会抽奖、不会改地址。**

### 两个已知的官方约束（本项目因此而被设计成现在这样）

- **`calculate-price` 返回的金额单位是「分」**。本项目虽然不调用它，但把这条事实
  作为整个代码库的金额口径：`src/mcd_adapter.py` 把所有金额统一收敛成整数「分」，
  展示层再 ÷100。换算用 `decimal.Decimal` + `ROUND_HALF_UP`，不走二进制浮点
  （`12.345 * 100` 在浮点下是 `1234.4999…`，直接取整会少算一分钱）。
- **一单只能使用一张优惠券**，传两张会返回错误码 `600022`。本项目只读，不会触发它；
  这里列出来是为了说明「为什么不顺手做个券优化」——那需要下单，超出了本项目的只读边界。

---

## 3. 调用流程

```text
用户                Agent（WorkBuddy / 任意 MCP 客户端）            本项目 mcd-life-report
 │                            │                                        │
 │  "看看我的点餐人生"          │                                        │
 ├───────────────────────────▶│                                        │
 │                            │ ① order-list                           │
 │                            ├──────────▶ mcp.mcd.cn（只读）           │
 │                            │◀────────── 最近 10 单 JSON              │
 │                            │                                        │
 │                            │ ② now-time-info（校对时钟）              │
 │                            ├──────────▶ mcp.mcd.cn                  │
 │                            │◀────────── 服务端时间                    │
 │                            │                                        │
 │                            │ ③ 把 ① 的 JSON 原样落盘 my-orders.json  │
 │                            │───────────────────────────────────────▶│ python demo.py --input my-orders.json
 │                            │                                        │   适配 → 分期 → 里程碑 → 旁白 → 长图
 │                            │◀───────────────────────────────────────│ out/mcd-life-report.png (1200×2200)
 │◀───────────────────────────┤ 报告文本 + 长图                          │
```

关键设计取舍：**本项目自身不实现 MCP 网络客户端**。

- 原因一：MCP 的 Streamable HTTP 会话、鉴权、重试属于「Agent 宿主」的职责，
  重复实现一遍只会引入第二份容易过期的协议代码；
- 原因二：本项目承诺**零第三方依赖**（只允许标准库）与**离线可跑**，
  任何网络客户端都无法被离线 demo 覆盖到测试；
- 原因三（最重要）：**未经真实 Token 验证的网络代码不应该出现在参赛仓库里**。
  与其写一段「看起来能跑」的 HTTP 客户端，不如把边界说清楚：
  本项目消费 `order-list` 的 JSON 输出，并把这段输出忠实、可验证地变成一份人生编年史。

因此 `demo.py` 的 `--input` 是唯一的真实数据入口；Agent 侧的调用方式见
[`SKILL.md`](SKILL.md)。

---

## 4. `order-list` → 内部模型的字段映射

`order-list` 是**无分页参数、只返回最近 10 单**的接口。适配层
（`src/mcd_adapter.py`）的做法是：**多候选键容错 + 缺失即标注**，绝不猜值。

| 内部字段 | 候选键（按优先级） | 核实状态 | 缺失时的行为 |
|---|---|---|---|
| `order_id` | `orderId` / `order_id` / `orderNo` / `order_no` / `orderNumber` / `id` | ⚠️ 键名未核实 | 记为 `UNKNOWN-ORDER` 并写入 `missing_fields` |
| `created_at` | `orderTime` / `order_time` / `createTime` / `createdAt` / `created_at` / `payTime` / `orderDate` / `time` | ⚠️ 键名未核实 | `created_at = None`，该单被标为「未分期」，并在报告里计数 |
| `amount_cents` | `payAmount` / `pay_amount` / `actualAmount` / `orderAmount` / `totalAmount` / `payPrice` / `totalPrice` / `amount` / `price` | ⚠️ 键名未核实；**单位按「分」**处理（与官方对 `calculate-price` 的口径一致） | 按 0 处理并写入 `missing_fields`（不会静默当成真实消费） |
| `refund_cents` | `refundAmount` / `refund_amount` / `refundPrice` / `refund` | ⚠️ 键名未核实 | 退化为「状态文本兜底判据」，见下 |
| `refund_cents`（兜底） | `isRefunded` / `is_refunded` / `refunded`（布尔） | ⚠️ 键名未核实 | — |
| `refund_cents`（状态兜底） | 无退款金额字段时，检查 `orderStatus` / `status` 是否含「退款 / 退货 / 已取消 / 已撤销 / refund / cancel / closed」 | ⚠️ 关键词表为本项目定义 | 命中即视为整单退款，**并在报告中明说这条判据是兜底逻辑，建议自行核对** |
| `store_name` | `storeName` / `store_name` / `shopName` / `store` / `restaurant` | ⚠️ 键名未核实 | 展示为「门店未核实」 |
| `items[]` | `products` / `items` / `productList` / `product_list` / `orderItems` / `order_items` / `goods` / `itemList` / `detail` | ⚠️ 键名未核实 | 商品明细缺失，相关里程碑标为「未核实」 |
| `items[].name` | `productName` / `product_name` / `name` / `skuName` / `title` / `goodsName` | ⚠️ 键名未核实 | 占位为「未标注商品」 |
| `items[].quantity` | `quantity` / `num` / `qty` / `count` / `number` | ⚠️ 键名未核实 | 默认 1（数量缺失时取最小合理值，并计入缺失统计） |
| `items[].price` | `price` / `unitPrice` / `unit_price` / `productPrice` / `amount` / `salePrice` | ⚠️ 键名未核实 | `None`，不参与金额计算 |
| `items[].category` | `category` / `categoryName` / `category_name` / `productType` / `type` / `tag` | ⚠️ 键名未核实 | 占位为「未分类」；全表无品类时「探索期」**降级**为按商品名判定并打印警告 |

**关于「⚠️ 键名未核实」**：官方文档确认了服务地址、协议、鉴权、限流与若干业务约束，
但**没有**公开 `order-list` 的字段名清单。因此本适配层不做任何单一键名的硬编码假设，
而是「多候选键命中 + 全部未命中就记为缺失并告知用户」。
这是一条刻意的工程选择：**宁可报告「未核实」，也不编一个字段名。**

外层信封的容错同样如此（`unwrap_payload`）：支持
`{"data": {"orders": [...]}}`、`{"data": [...]}`、`{"result": {...}}`、`{"orders": [...]}`、
裸数组、以及「整个响应就是一条订单」；如果都不匹配，**直接抛错并列出尝试过的容器键**，
而不是返回空列表假装成功。

### 退款口径（写进代码与测试的第一原则）

- **整单退款**（实付 − 退款 ≤ 0）：**不计入任何金额指标**——累计消费、单笔最大金额、
  最贵的深夜订单、平均客单价、最长连续天数全部剔除；
- 但它**保留在时间轴上**，以空心点 + 斜线标记，因为「发生过的事不能被抹掉」；
- **部分退款**：按净额（实付 − 退款）计入；
- 退款额超过实付额时按实付额截断（防御脏数据）。

---

## 5. 业务价值

### 对用户

- **零输入**：不用填问卷、不用授权额外数据，只读最近 10 单就能得到一份可分享的「人生编年史」；
- **可分享物**：1200×2200 的长图，天然适配朋友圈 / 微信 / 小红书，
  这是把「一个功能」变成「一次传播」的关键；
- **可核对**：每一条结论都带判据（「距上一单 48.2 天（≥ 30 天），断档一个自然月后又回来了」），
  用户可以一眼判断这个结论是不是胡说。

### 对麦当劳 / 生态

- **激活历史订单资产**：`order-list` 是只读接口，本身不产生交易价值；
  本项目把它变成「有情绪价值的复述」，是典型的「让已有数据长出二次触点」；
- **天然的分享裂变**：长图自带数据来源与「非官方产品」声明，传播路径干净、合规；
- **零风险**：只用只读工具，不触碰下单、券、地址等写操作，
  不消耗库存、不产生资金流，接入成本极低；
- **可复用的基础设施**：`src/` 是一段纯函数式管道，
  任何「想用订单数据讲点别的故事」的 Skill 都能直接复用适配层与分期层。

---

## 6. 错误处理与限流

| 情况 | 本项目的行为 |
|---|---|
| `401` Token 无效/过期/未提供 | 属于 Agent 宿主侧的鉴权问题；本项目只消费落盘的 JSON，因此不受影响。配置侧请检查 `MCD_MCP_TOKEN` |
| `429` 触发限流（>600 次/分钟） | 本项目不发起请求，天然不会触发；建议 Agent 侧对 `order-list` 采取「一次会话一次调用」策略 |
| `600022` 一单多券 | 只读场景不会出现；本项目不做券操作，故不涉及 |
| 响应结构不认识 | 适配层抛 `MCDAdapterError`，消息中列出已尝试的容器键，**不静默返回空数据** |
| 字段缺失 | 记入 `missing_fields` / `missing_field_summary`，在终端报告与长图上以「未核实」呈现 |
| 时间为空 / 无法解析 | 该单被标为「未分期」并计数，其余订单照常分期，不因为一单脏数据整体失败 |
| 截图式「有数据但全为 0」 | 报告会同时给出实付总额与退款总额，用户可以一眼核对差额是否来自退款 |

---

## 7. 合规与隐私

- **不写入任何账户信息**：`query-my-account` 的返回不进入报告、不进入长图、不落盘；
- **不落盘真实凭证**：Token 只从环境变量读取，仓库内没有任何真实凭证；
- **fixtures 全部为合成数据**：见 [`fixtures/README.md`](fixtures/README.md)，
  终端输出、长图页脚与 README 三处都做了「合成示例数据」标注；
- **不使用官方视觉素材**：长图里的图形全部由代码绘制，不使用麦当劳 Logo、IP 形象或官方图片；
- **口径声明内嵌在产物里**：终端报告的「诚实性说明与已知限制」区块与长图的对应区块，
  保证了「脱离仓库单独传播的那张图」也带着数据来源与限制说明。
