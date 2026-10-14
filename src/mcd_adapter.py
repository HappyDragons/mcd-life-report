"""麦当劳 MCP ``order-list`` 响应适配层。

职责边界（很重要，不要再往外扩）：

1. **字段映射**：把 MCP 返回的原始 JSON 映射成本仓库内部的 :class:`Order` / :class:`OrderItem`。
   MCP 侧字段名以「多候选键」的方式容错匹配，任何一个候选键命中即可；全部未命中时
   记为缺失并写入 :attr:`Order.missing_fields`，由上层如实告知用户「未核实」，
   **绝不用猜测值填充**。
2. **金额单位换算**：MCP 的金额单位是「分」（官方文档已确认 ``calculate-price`` 返回分）。
   本适配层把所有金额统一收敛成整数「分」，展示层再 ÷100。若数据源确实返回「元」，
   由调用方显式传入 ``amount_unit="yuan"``，不做数值猜测。
3. **退款过滤**：整单退款（实付 − 退款 ≤ 0）的订单**不得计入消费额**，
   但仍保留在序列里并可被时间轴以「已退款」样式标记（发生过就是发生过，不能抹掉）。
   部分退款按净额计入。
4. **窗口提示**：MCP ``order-list`` **无分页参数，只返回最近 10 单**。适配层因此固定
   暴露 :data:`ORDER_LIST_WINDOW` 常量，并在窗口被填满时把 ``may_be_truncated``
   置为 ``True``，提醒上层"这不是完整消费史"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: MCP 官方文档明确说明：``order-list`` 无分页参数，只返回最近 10 单。
ORDER_LIST_WINDOW = 10

#: MCP 服务端时间口径按中国标准时间处理（固定 +08:00，避免依赖 zoneinfo 以兼容 Python 3.8）。
CST = timezone(timedelta(hours=8))

#: 字段候选键。顺序即优先级，先命中的优先。命名基线来自 order-list 常见返回结构，
#: 未核实的候选键已在 MCP_INTEGRATION.md 中标注。
FIELD_CANDIDATES: Dict[str, Tuple[str, ...]] = {
    "order_id": ("orderId", "order_id", "orderNo", "order_no", "orderNumber", "id"),
    "created_at": (
        "orderTime",
        "order_time",
        "createTime",
        "createdAt",
        "created_at",
        "payTime",
        "orderDate",
        "time",
    ),
    "amount": (
        "payAmount",
        "pay_amount",
        "actualAmount",
        "orderAmount",
        "totalAmount",
        "payPrice",
        "totalPrice",
        "amount",
        "price",
    ),
    "refund": ("refundAmount", "refund_amount", "refundPrice", "refund"),
    "store": ("storeName", "store_name", "shopName", "store", "restaurant"),
    "products": (
        "products",
        "items",
        "productList",
        "product_list",
        "orderItems",
        "order_items",
        "goods",
        "itemList",
        "detail",
    ),
    "status": ("orderStatus", "order_status", "status", "payStatus", "refundStatus"),
    "refunded_flag": ("isRefunded", "is_refunded", "refunded"),
}

#: 商品条目内层的候选键。
ITEM_FIELD_CANDIDATES: Dict[str, Tuple[str, ...]] = {
    "name": ("productName", "product_name", "name", "skuName", "title", "goodsName"),
    "quantity": ("quantity", "num", "qty", "count", "number"),
    "price": ("price", "unitPrice", "unit_price", "productPrice", "amount", "salePrice"),
    "category": ("category", "categoryName", "category_name", "productType", "type", "tag"),
}

#: 状态文本中表示「整单已退款 / 已取消」的关键词。
#: 仅在**没有**显式退款金额字段时作为兜底判据使用（命中即视为整单退款）。
REFUND_STATUS_KEYWORDS = ("退款", "退货", "已取消", "已撤销", "refund", "cancel", "closed")

#: 多个容器键出现时的解包顺序（``payload`` 可能多层嵌套）。
ENVELOPE_KEYS = ("data", "result", "body", "payload")

#: 可能的订单数组键名。
LIST_KEYS = (
    "orders",
    "orderList",
    "order_list",
    "list",
    "records",
    "items",
    "rows",
    "data",
)

#: 金额单位到「分」的倍率。
AMOUNT_UNIT_MULTIPLIER: Dict[str, int] = {"cent": 1, "yuan": 100}


class MCDAdapterError(ValueError):
    """适配层无法理解的输入结构时抛出，附带可读的中文原因。"""


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #


def pick(raw: Dict[str, Any], candidates: Sequence[str]) -> Optional[Any]:
    """从 ``raw`` 中按候选键顺序取出第一个「非空」值。

    参数:
        raw: 原始字典。
        candidates: 候选键序列，顺序即优先级。

    返回:
        命中的值；全部未命中或命中的值均为 ``None`` / 空字符串时返回 ``None``。

    说明:
        ``0`` 与 ``False`` 被视为**有效值**（金额为 0、退款标记为 False 都是有意义的事实），
        只有 ``None`` 与空白字符串才被当作缺失。
    """
    for key in candidates:
        if key in raw:
            value = raw[key]
            if value is None:
                continue
            if isinstance(value, str) and value.strip() == "":
                continue
            return value
    return None


def parse_timestamp(value: Any) -> Optional[datetime]:
    """把 MCP 侧的时间字段解析成北京时间（naive datetime，tzinfo 已剥离）。

    支持四类写法：
    - ISO 8601 含时区：``2026-05-04T12:20:00+08:00``
    - ISO 8601 无时区：``2026-05-04T12:20:00`` / ``2026-05-04 12:20:00``
    - 仅日期：``2026-05-04``
    - 秒级 / 毫秒级 Unix 时间戳（``int`` / ``float`` / 纯数字字符串）

    参数:
        value: 原始时间表示。

    返回:
        北京时间口径的 ``datetime``；无法解析时返回 ``None``（上层记为「未核实」）。

    说明:
        带时区的输入统一换算到固定 +08:00 后再剥离 tzinfo，因此同一份数据在任何
        机器上跑出来的分期结果完全一致（不依赖本机时区）。
    """
    if value is None:
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return _from_epoch(float(value))

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return _from_epoch(float(text))
        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y-%m-%d %H:%M"):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(CST).replace(tzinfo=None)
        return parsed

    return None


def _from_epoch(value: float) -> datetime:
    """把 Unix 时间戳（秒或毫秒）转成北京时间 naive datetime。"""
    # 大于 1e11 的数值按毫秒处理（1e11 秒 ≈ 公元 5138 年，真实订单不会到那个量级）。
    seconds = value / 1000.0 if value > 1e11 else value
    return datetime.fromtimestamp(seconds, tz=CST).replace(tzinfo=None)


def to_cents(value: Any, unit: str = "cent") -> Optional[int]:
    """把金额换算成整数「分」。

    参数:
        value: 金额原始值（可能是 int / float / 数字字符串）。
        unit: ``"cent"``（默认，MCP 口径）或 ``"yuan"``。

    返回:
        整数分；无法换算时返回 ``None``。

    说明:
        换算使用 ``decimal.Decimal``（按 ``ROUND_HALF_UP`` 进位到分），而不是二进制浮点。
        原因是二进制浮点会算错钱：``12.345 * 100`` 得到的是 ``1234.4999999999998``，
        直接取整会少一分。本函数不做任何「猜单位」的启发式——单位错了是调用方的事。
    """
    if unit not in AMOUNT_UNIT_MULTIPLIER:
        raise MCDAdapterError("amount_unit 只支持 'cent' 或 'yuan'，收到：%r" % (unit,))
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip().replace("¥", "").replace("￥", "").replace(",", "")
        if text == "":
            return None
    else:
        return None

    try:
        decimal_value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None

    scaled = decimal_value * AMOUNT_UNIT_MULTIPLIER[unit]
    return int(scaled.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_yuan(cents: Optional[int]) -> str:
    """把「分」格式化成人类可读的人民币字符串。

    参数:
        cents: 整数分；``None`` 表示未核实。

    返回:
        形如 ``"¥344.50"`` 的字符串；``None`` 返回 ``"未核实"``。
    """
    if cents is None:
        return "未核实"
    return "¥%.2f" % (cents / 100.0)


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #


@dataclass
class OrderItem:
    """一笔订单里的单个商品行。"""

    name: str
    """商品名；字段缺失时为 ``"未标注商品"``。"""

    quantity: int
    """数量，至少为 1。"""

    unit_price_cents: Optional[int]
    """单价（分）；缺失为 ``None``。"""

    category: str
    """品类；字段缺失时为 ``"未分类"``。"""

    @property
    def line_total_cents(self) -> Optional[int]:
        """该商品行金额（分）；单价缺失时返回 ``None``。"""
        if self.unit_price_cents is None:
            return None
        return self.unit_price_cents * self.quantity


@dataclass
class Order:
    """一笔规范化后的麦当劳订单。"""

    order_id: str
    """订单号（合成数据用 ``SYN-xxxx``，真实数据绝不写入仓库）。"""

    created_at: Optional[datetime]
    """下单时间（北京时间，naive）。"""

    amount_cents: int
    """实付金额（分，正数）。"""

    refund_cents: int
    """退款金额（分，正数；无退款为 0）。"""

    store_name: Optional[str]
    """门店名；缺失为 ``None``（展示为「未核实」）。"""

    items: List[OrderItem] = field(default_factory=list)

    status_text: Optional[str] = None
    """原始状态文本，仅用于排查与诚实说明。"""

    refund_source: str = "none"
    """退款判据来源：``"amount"``（显式退款金额）/ ``"status"``（状态文本兜底）/ ``"none"``。"""

    missing_fields: List[str] = field(default_factory=list)
    """本条订单缺失的字段名列表（例如 ``["created_at", "store"]``），供上层如实播报。"""

    raw: Dict[str, Any] = field(default_factory=dict)
    """原始字典，便于调试；不会被写进报告输出。"""

    @property
    def net_cents(self) -> int:
        """净消费额（分）= 实付 − 退款，下限为 0。

        整单退款时等于 0，因此天然满足「整单退款不得计入消费额」。
        """
        return max(self.amount_cents - self.refund_cents, 0)

    @property
    def is_fully_refunded(self) -> bool:
        """是否整单退款（净额 ≤ 0 且原实付 > 0）。"""
        return self.amount_cents > 0 and self.net_cents <= 0

    @property
    def is_partially_refunded(self) -> bool:
        """是否部分退款（0 < 退款 < 实付）。"""
        return 0 < self.refund_cents < self.amount_cents

    @property
    def counted_in_spend(self) -> bool:
        """该单是否计入「累计消费」。按定义：整单退款不计入。"""
        return not self.is_fully_refunded

    @property
    def categories(self) -> List[str]:
        """该单涉及的品类集合（去重、保持出现顺序）。"""
        seen: List[str] = []
        for item in self.items:
            if item.category not in seen:
                seen.append(item.category)
        return seen

    @property
    def item_names(self) -> List[str]:
        """该单涉及的商品名集合（去重、保持出现顺序）。"""
        seen: List[str] = []
        for item in self.items:
            if item.name not in seen:
                seen.append(item.name)
        return seen

    @property
    def total_quantity(self) -> int:
        """该单商品总件数。"""
        return sum(item.quantity for item in self.items)


# --------------------------------------------------------------------------- #
# 适配入口
# --------------------------------------------------------------------------- #


def unwrap_payload(payload: Any) -> List[Dict[str, Any]]:
    """把 MCP ``order-list`` 响应解包成订单字典列表。

    参数:
        payload: MCP 工具返回的任意 JSON 结构。

    返回:
        订单原始字典列表。

    异常:
        MCDAdapterError: 结构无法识别时抛出，消息里会列出可接受的形状。

    说明:
        容错的只是「外层容器」，**不猜语义**：如果一个字典里既没有订单数组键、
        又不像单条订单（没有金额或时间字段），就直接报错，而不是返回空列表假装成功。
    """
    if payload is None:
        raise MCDAdapterError("order-list 响应为 None，无法适配。")

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if not isinstance(payload, dict):
        raise MCDAdapterError("order-list 响应既不是对象也不是数组，实际类型：%s" % type(payload).__name__)

    # 逐层剥信封：data / result / body / payload，最多下钻 6 层，防止畸形数据死循环。
    node: Any = payload
    for _ in range(6):
        if isinstance(node, list):
            return [item for item in node if isinstance(item, dict)]
        if not isinstance(node, dict):
            break

        for key in LIST_KEYS:
            value = node.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]

        moved = False
        for key in ENVELOPE_KEYS:
            value = node.get(key)
            if isinstance(value, (dict, list)):
                node = value
                moved = True
                break
        if not moved:
            break

    # 兜底：整个响应本身就是一条订单（含金额或时间字段即认可）。
    if isinstance(node, dict) and (
        any(k in node for k in FIELD_CANDIDATES["amount"])
        or any(k in node for k in FIELD_CANDIDATES["created_at"])
    ):
        return [node]

    raise MCDAdapterError(
        "无法在 order-list 响应中定位订单数组。已尝试的容器键：%s；"
        "若返回结构特殊，请先解包成订单数组再调用。" % ", ".join(LIST_KEYS)
    )


def adapt_item(raw: Dict[str, Any], amount_unit: str = "cent") -> OrderItem:
    """把单个商品条目字典适配成 :class:`OrderItem`。

    参数:
        raw: 商品原始字典。
        amount_unit: 金额单位，``"cent"`` 或 ``"yuan"``。

    返回:
        :class:`OrderItem`；缺失字段用明确的占位值（``"未标注商品"`` / ``1`` / ``None`` / ``"未分类"``）。
    """
    name = pick(raw, ITEM_FIELD_CANDIDATES["name"])
    quantity = pick(raw, ITEM_FIELD_CANDIDATES["quantity"])
    price = pick(raw, ITEM_FIELD_CANDIDATES["price"])
    category = pick(raw, ITEM_FIELD_CANDIDATES["category"])

    try:
        quantity_int = max(int(quantity), 1) if quantity is not None else 1
    except (TypeError, ValueError):
        quantity_int = 1

    return OrderItem(
        name=str(name).strip() if isinstance(name, str) and name.strip() else "未标注商品",
        quantity=quantity_int,
        unit_price_cents=to_cents(price, amount_unit),
        category=str(category).strip() if isinstance(category, str) and category.strip() else "未分类",
    )


def adapt_order(raw: Dict[str, Any], amount_unit: str = "cent") -> Order:
    """把单条 ``order-list`` 订单字典适配成 :class:`Order`。

    参数:
        raw: 单条订单原始字典。
        amount_unit: 金额单位，``"cent"``（MCP 默认口径）或 ``"yuan"``。

    返回:
        :class:`Order`。金额缺失时按 0 处理并记入 ``missing_fields``（上层会如实播报，
        不会把它悄悄当成一笔真实消费）。

    异常:
        MCDAdapterError: 传入的不是字典。
    """
    if not isinstance(raw, dict):
        raise MCDAdapterError("单条订单必须是对象，实际类型：%s" % type(raw).__name__)

    missing: List[str] = []

    order_id_raw = pick(raw, FIELD_CANDIDATES["order_id"])
    order_id = str(order_id_raw).strip() if order_id_raw is not None else ""
    if not order_id:
        order_id = "UNKNOWN-ORDER"
        missing.append("order_id")

    created_raw = pick(raw, FIELD_CANDIDATES["created_at"])
    created_at = parse_timestamp(created_raw)
    if created_at is None:
        missing.append("created_at")

    amount_raw = pick(raw, FIELD_CANDIDATES["amount"])
    amount_cents = to_cents(amount_raw, amount_unit)
    if amount_cents is None:
        amount_cents = 0
        missing.append("amount_cents")
    else:
        amount_cents = max(amount_cents, 0)

    store_raw = pick(raw, FIELD_CANDIDATES["store"])
    store_name = str(store_raw).strip() if isinstance(store_raw, str) and store_raw.strip() else None
    if store_name is None:
        missing.append("store_name")

    products_raw = pick(raw, FIELD_CANDIDATES["products"])
    items: List[OrderItem] = []
    if isinstance(products_raw, list):
        for entry in products_raw:
            if isinstance(entry, dict):
                items.append(adapt_item(entry, amount_unit))
    if not items:
        missing.append("items")

    status_raw = pick(raw, FIELD_CANDIDATES["status"])
    status_text = str(status_raw).strip() if status_raw is not None else None

    refund_raw = pick(raw, FIELD_CANDIDATES["refund"])
    refund_cents = to_cents(refund_raw, amount_unit)
    refund_source = "none"

    if refund_cents is None:
        refunded_flag = pick(raw, FIELD_CANDIDATES["refunded_flag"])
        if refunded_flag is True or (isinstance(refunded_flag, str) and refunded_flag.strip().lower() in ("true", "1", "yes")):
            # 只有布尔型「已退款」标记时，视为整单退款。
            refund_cents = amount_cents
            refund_source = "amount"
        elif status_text and any(k in status_text.lower() for k in REFUND_STATUS_KEYWORDS):
            # 兜底判据：状态文本里出现退款/取消关键词，且没有任何退款金额字段。
            refund_cents = amount_cents
            refund_source = "status"
        else:
            refund_cents = 0
    else:
        refund_source = "amount"

    refund_cents = min(max(refund_cents, 0), amount_cents) if amount_cents > 0 else 0

    return Order(
        order_id=order_id,
        created_at=created_at,
        amount_cents=amount_cents,
        refund_cents=refund_cents,
        store_name=store_name,
        items=items,
        status_text=status_text,
        refund_source=refund_source,
        missing_fields=missing,
        raw=raw,
    )


def adapt_orders(
    payload: Any,
    amount_unit: str = "cent",
) -> Tuple[List[Order], Dict[str, Any]]:
    """把 ``order-list`` 响应整体适配成订单列表 + 数据质量元信息。

    参数:
        payload: MCP ``order-list`` 返回的原始 JSON。
        amount_unit: 金额单位，``"cent"`` 或 ``"yuan"``。

    返回:
        ``(orders, meta)`` 二元组：

        - ``orders``：按时间升序排列、已去重的 :class:`Order` 列表；
          **时间无法解析的订单排在最后**（而不是被丢弃），并在 meta 里计数。
        - ``meta``：包含 ``raw_count`` / ``order_count`` / ``duplicated`` /
          ``dropped_no_time`` / ``window`` / ``may_be_truncated`` / ``fully_refunded`` /
          ``missing_field_summary`` 的字典。

    说明:
        - **去重**：同一 ``order_id`` 只保留第一条，重复次数记入 meta。
        - **不丢弃**：解析不出时间的订单保留在末尾，meta 里明确计数，由上层如实说明。
        - **窗口**：``raw_count >= 10`` 时把 ``may_be_truncated`` 置为 True。
    """
    raw_orders = unwrap_payload(payload)

    seen_ids: set = set()
    orders: List[Order] = []
    duplicated = 0

    for raw in raw_orders:
        order = adapt_order(raw, amount_unit)
        if order.order_id in seen_ids:
            duplicated += 1
            continue
        seen_ids.add(order.order_id)
        orders.append(order)

    with_time = [o for o in orders if o.created_at is not None]
    without_time = [o for o in orders if o.created_at is None]
    with_time.sort(key=lambda o: o.created_at)  # type: ignore[arg-type,return-value]

    missing_summary: Dict[str, int] = {}
    for order in orders:
        for name in order.missing_fields:
            missing_summary[name] = missing_summary.get(name, 0) + 1

    meta: Dict[str, Any] = {
        "raw_count": len(raw_orders),
        "order_count": len(orders),
        "duplicated": duplicated,
        "dropped_no_time": len(without_time),
        "window": ORDER_LIST_WINDOW,
        "may_be_truncated": len(orders) >= ORDER_LIST_WINDOW,
        "fully_refunded": sum(1 for o in orders if o.is_fully_refunded),
        "missing_field_summary": missing_summary,
        "amount_unit": amount_unit,
    }
    return with_time + without_time, meta


def load_orders_from_file(path: str, amount_unit: str = "cent") -> Tuple[List[Order], Dict[str, Any]]:
    """从本地 JSON 文件读取并适配订单（离线 demo 与真实导出的统一入口）。

    参数:
        path: JSON 文件路径。允许两种顶层形态：``{"orders": [...]}`` 或裸数组。
        amount_unit: 金额单位。

    返回:
        同 :func:`adapt_orders`。

    异常:
        MCDAdapterError: 文件不存在或不是合法 JSON。
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        raise MCDAdapterError("找不到订单数据文件：%s" % path)
    except json.JSONDecodeError as exc:
        raise MCDAdapterError("订单数据文件不是合法 JSON：%s（%s）" % (path, exc))

    return adapt_orders(payload, amount_unit)


def collect_orders(orders: Iterable[Order]) -> Dict[str, List[Order]]:
    """按「是否计入消费」把订单分成两组，供上层统计与展示。

    参数:
        orders: 订单序列。

    返回:
        ``{"counted": [...], "fully_refunded": [...]}``。
    """
    counted: List[Order] = []
    refunded: List[Order] = []
    for order in orders:
        if order.counted_in_spend:
            counted.append(order)
        else:
            refunded.append(order)
    return {"counted": counted, "fully_refunded": refunded}
