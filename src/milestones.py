"""里程碑提取：从订单序列里挑出「值得写进人生简历」的那几件事。

本模块提取六个硬性里程碑（全部为确定性计算，无随机、无估算）：

==================== ====================================================
里程碑               口径
==================== ====================================================
第一次点单           窗口内时间最早的一单（含已退款订单——点过就是点过）。
最长连续天数         日历天连续有**有效消费**的订单的最长天数（整单退款的
                     订单不计入，因为那一单最终没有发生消费）。
单笔最大金额         计入消费的订单里净额最大的一单；并列时取更早的一单。
最贵的深夜订单       22:00–04:59 下单、计入消费的订单里净额最大的一单；
                     并列时取更早的一单。窗口内没有深夜订单时为「未核实」。
累计消费             计入消费订单的净额之和（实付 − 退款），同时给出实付总额
                     与退款总额，方便用户核对差额。
最常点餐品           计入消费的订单里数量最多的商品；并列时依次比较
                     「出现的订单数」「首次出现时间」「名称字典序」。
==================== ====================================================

「整单退款的订单不得计入消费额」是本模块的第一原则：它既影响累计消费，
也影响单笔最大金额 / 最贵深夜订单 / 平均客单价 / 最长连续天数。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Sequence

from .chronicle import SLOT_NIGHT_END_HOUR, SLOT_NIGHT_START_HOUR, is_night_order
from .mcd_adapter import Order, format_yuan


@dataclass
class Milestone:
    """一条里程碑。``available=False`` 表示窗口内无法计算，必须如实显示原因。"""

    key: str
    """里程碑标识（英文）。"""

    title: str
    """中文标题。"""

    value: Optional[Any] = None
    """数值结果；无法计算时为 ``None``。"""

    display: str = "未核实"
    """展示字符串（人民币已格式化、天数带单位）。"""

    detail: str = ""
    """一行补充说明（含口径或原因）。"""

    available: bool = False
    """是否成功算出。"""

    order_index: Optional[int] = None
    """相关订单在序列中的下标（用于时间轴标注）。"""

    order_ids: List[str] = field(default_factory=list)
    """相关订单号。"""

    def as_dict(self) -> Dict[str, Any]:
        """转成可 JSON 序列化的字典。"""
        return {
            "key": self.key,
            "title": self.title,
            "value": self.value,
            "display": self.display,
            "detail": self.detail,
            "available": self.available,
            "order_index": self.order_index,
            "order_ids": list(self.order_ids),
        }


def _unavailable(key: str, title: str, reason: str) -> Milestone:
    """构造一条「无法计算」的里程碑。"""
    return Milestone(key=key, title=title, value=None, display="未核实", detail=reason, available=False)


def longest_streak(orders: Sequence[Order]) -> Dict[str, Any]:
    """计算最长连续点单天数。

    参数:
        orders: 订单序列。

    返回:
        字典，含 ``days``（最长连续天数，无有效订单时为 0）、``start`` / ``end``
        （起止日期字符串或 ``None``）、``dates``（该连续段的日期列表）。

    说明:
        只统计**计入消费**的订单（即整单退款订单被排除）。同一天多单只算一天。
    """
    days_set = sorted(
        {o.created_at.date() for o in orders if o.created_at is not None and o.counted_in_spend}
    )
    if not days_set:
        return {"days": 0, "start": None, "end": None, "dates": []}

    best_start = best_end = days_set[0]
    best_len = 1
    current_start = days_set[0]
    current_len = 1

    for previous, current in zip(days_set, days_set[1:]):
        if (current - previous).days == 1:
            current_len += 1
        else:
            current_start = current
            current_len = 1
        if current_len > best_len:
            best_len = current_len
            best_start = current_start
            best_end = current

    day_lookup = set(days_set)
    span_dates: List[date] = []
    cursor = best_start
    while cursor <= best_end:
        if cursor in day_lookup:
            span_dates.append(cursor)
        cursor = date.fromordinal(cursor.toordinal() + 1)

    return {
        "days": best_len,
        "start": best_start.isoformat(),
        "end": best_end.isoformat(),
        "dates": [d.isoformat() for d in span_dates],
    }


def most_ordered_item(orders: Sequence[Order]) -> Dict[str, Any]:
    """统计最常点餐品。

    参数:
        orders: 订单序列。

    返回:
        字典，含 ``name``（无数据时为 ``None``）、``quantity``、``orders``（出现订单数）、
        ``first_seen``（首次出现时间字符串或 ``None``）、``rank``（完整排行，取前 10）。

    说明:
        只统计计入消费的订单。排序键为 ``(-总数量, -出现订单数, 首次出现时间, 名称)``，
        因此结果唯一且可复现。
    """
    tally: Dict[str, Dict[str, Any]] = {}
    for index, order in enumerate(orders):
        if not order.counted_in_spend or order.created_at is None:
            continue
        for item in order.items:
            entry = tally.setdefault(
                item.name,
                {"quantity": 0, "orders": 0, "first_seen": order.created_at, "first_index": index},
            )
            entry["quantity"] += item.quantity
            entry["orders"] += 1
            if order.created_at < entry["first_seen"]:
                entry["first_seen"] = order.created_at
                entry["first_index"] = index

    if not tally:
        return {"name": None, "quantity": 0, "orders": 0, "first_seen": None, "rank": []}

    ranked = sorted(
        tally.items(),
        key=lambda pair: (-pair[1]["quantity"], -pair[1]["orders"], pair[1]["first_seen"], pair[0]),
    )
    top_name, top_entry = ranked[0]

    rank = [
        {
            "name": name,
            "quantity": entry["quantity"],
            "orders": entry["orders"],
            "first_seen": entry["first_seen"].isoformat(sep=" "),
        }
        for name, entry in ranked[:10]
    ]

    return {
        "name": top_name,
        "quantity": top_entry["quantity"],
        "orders": top_entry["orders"],
        "first_seen": top_entry["first_seen"].isoformat(sep=" "),
        "first_index": top_entry["first_index"],
        "rank": rank,
    }


def extract_milestones(
    orders: Sequence[Order],
    night_start_hour: int = SLOT_NIGHT_START_HOUR,
    night_end_hour: int = SLOT_NIGHT_END_HOUR,
) -> Dict[str, Milestone]:
    """提取全部里程碑。

    参数:
        orders: 订单序列（升序）。
        night_start_hour: 深夜起点小时（含），默认 22。
        night_end_hour: 深夜终点小时（不含），默认 5。

    返回:
        字典，键为里程碑标识：``first_order`` / ``longest_streak`` / ``biggest_order`` /
        ``priciest_night_order`` / ``total_spend`` / ``most_ordered_item``。
        任何算不出来的里程碑都会返回 ``available=False`` 并给出中文原因，绝不静默填 0。

    说明:
        空序列不会抛异常，而是返回六条全部「未核实」的里程碑——因为「没有数据」本身
        就是一条需要如实告知用户的事实。
    """
    result: Dict[str, Milestone] = {}

    if not orders:
        reason = "订单窗口为空，无法计算。"
        for key, title in (
            ("first_order", "第一次点单"),
            ("longest_streak", "最长连续天数"),
            ("biggest_order", "单笔最大金额"),
            ("priciest_night_order", "最贵的深夜订单"),
            ("total_spend", "累计消费"),
            ("most_ordered_item", "最常点餐品"),
        ):
            result[key] = _unavailable(key, title, reason)
        return result

    # ---------------- 第一次点单 ----------------
    timed = [(index, order) for index, order in enumerate(orders) if order.created_at is not None]
    if timed:
        index, order = timed[0]
        refund_note = "（该单已整单退款）" if order.is_fully_refunded else ""
        result["first_order"] = Milestone(
            key="first_order",
            title="第一次点单",
            value=order.created_at.isoformat(sep=" "),
            display=order.created_at.strftime("%Y-%m-%d %H:%M"),
            detail="实付 %s%s；%s" % (format_yuan(order.amount_cents), refund_note, order.store_name or "门店未核实"),
            available=True,
            order_index=index,
            order_ids=[order.order_id],
        )
    else:
        result["first_order"] = _unavailable("first_order", "第一次点单", "所有订单都缺少可解析的下单时间。")

    # ---------------- 最长连续天数 ----------------
    streak = longest_streak(orders)
    if streak["days"] > 0:
        result["longest_streak"] = Milestone(
            key="longest_streak",
            title="最长连续天数",
            value=streak["days"],
            display="%d 天" % streak["days"],
            detail=(
                "%s ~ %s，连续 %d 天有有效消费（整单退款的订单不计入）"
                % (streak["start"], streak["end"], streak["days"])
            ),
            available=True,
        )
    else:
        result["longest_streak"] = _unavailable(
            "longest_streak", "最长连续天数", "没有计入消费的订单，无法计算连续天数。"
        )

    # ---------------- 单笔最大金额 ----------------
    counted = [(index, order) for index, order in enumerate(orders) if order.counted_in_spend]
    if counted:
        best_index, best_order = max(counted, key=lambda pair: (pair[1].net_cents, -pair[0]))
        result["biggest_order"] = Milestone(
            key="biggest_order",
            title="单笔最大金额",
            value=best_order.net_cents,
            display=format_yuan(best_order.net_cents),
            detail="%s · %s · %s" % (
                best_order.created_at.strftime("%Y-%m-%d %H:%M") if best_order.created_at else "时间未核实",
                best_order.store_name or "门店未核实",
                "、".join("%s×%d" % (it.name, it.quantity) for it in best_order.items) or "商品明细缺失",
            ),
            available=True,
            order_index=best_index,
            order_ids=[best_order.order_id],
        )
    else:
        result["biggest_order"] = _unavailable(
            "biggest_order", "单笔最大金额", "窗口内没有有效消费订单（可能全部为整单退款）。"
        )

    # ---------------- 最贵的深夜订单 ----------------
    night = [
        (index, order)
        for index, order in counted
        if order.created_at is not None
        and is_night_order(order.created_at, night_start_hour, night_end_hour)
    ]
    if night:
        best_index, best_order = max(night, key=lambda pair: (pair[1].net_cents, -pair[0]))
        result["priciest_night_order"] = Milestone(
            key="priciest_night_order",
            title="最贵的深夜订单",
            value=best_order.net_cents,
            display=format_yuan(best_order.net_cents),
            detail="%s · 深夜口径 %02d:00–%02d:59 · %s"
            % (
                best_order.created_at.strftime("%Y-%m-%d %H:%M"),
                night_start_hour,
                (night_end_hour - 1) % 24,
                "、".join("%s×%d" % (it.name, it.quantity) for it in best_order.items) or "商品明细缺失",
            ),
            available=True,
            order_index=best_index,
            order_ids=[best_order.order_id],
        )
    else:
        result["priciest_night_order"] = _unavailable(
            "priciest_night_order",
            "最贵的深夜订单",
            "窗口内没有 %02d:00–%02d:00 的消费订单。" % (night_start_hour, night_end_hour),
        )

    # ---------------- 累计消费 ----------------
    gross = sum(o.amount_cents for o in orders)
    refunded = sum(o.refund_cents for o in orders)
    net = sum(o.net_cents for o in orders if o.counted_in_spend)
    refunded_orders = [o for o in orders if o.is_fully_refunded]
    detail = "实付合计 %s − 退款 %s = 净消费 %s" % (format_yuan(gross), format_yuan(refunded), format_yuan(net))
    if refunded_orders:
        detail += "；其中 %d 单整单退款已剔除" % len(refunded_orders)
    result["total_spend"] = Milestone(
        key="total_spend",
        title="累计消费",
        value=net,
        display=format_yuan(net),
        detail=detail,
        available=True,
    )

    # ---------------- 最常点餐品 ----------------
    top = most_ordered_item(orders)
    if top["name"]:
        tie_note = ""
        if len(top["rank"]) > 1 and top["rank"][1]["quantity"] == top["quantity"]:
            tie_note = "（与第 2 名数量并列，按出现订单数/首次出现时间决出）"
        result["most_ordered_item"] = Milestone(
            key="most_ordered_item",
            title="最常点餐品",
            value=top["name"],
            display="%s ×%d" % (top["name"], top["quantity"]),
            detail="出现在 %d 单里，首见于 %s%s" % (top["orders"], top["first_seen"], tie_note),
            available=True,
            order_index=top.get("first_index"),
        )
    else:
        result["most_ordered_item"] = _unavailable(
            "most_ordered_item", "最常点餐品", "窗口内没有含商品明细的有效消费订单。"
        )

    return result


def milestone_table(milestones: Dict[str, Milestone]) -> List[Dict[str, Any]]:
    """把里程碑字典整理成固定顺序的列表，供报告与长图逐行渲染。

    参数:
        milestones: :func:`extract_milestones` 的返回值。

    返回:
        固定顺序（第一次点单 → 最长连续天数 → 单笔最大金额 → 最贵的深夜订单 →
        累计消费 → 最常点餐品）的字典列表。
    """
    order = (
        "first_order",
        "longest_streak",
        "biggest_order",
        "priciest_night_order",
        "total_spend",
        "most_ordered_item",
    )
    return [milestones[key].as_dict() for key in order if key in milestones]


def average_ticket_cents(orders: Sequence[Order]) -> Optional[int]:
    """计算平均客单价（仅统计计入消费的订单）。

    参数:
        orders: 订单序列。

    返回:
        四舍五入到分的平均客单价；没有有效订单时返回 ``None``。

    说明:
        用 ``Decimal`` + ``ROUND_HALF_UP`` 而不是 ``round()``：``round`` 是银行家舍入，
        ``(3000 + 2001) / 2 = 2500.5`` 会被舍成 2500，涉钱的四舍五入应该是 2501。
    """
    values = [o.net_cents for o in orders if o.counted_in_spend]
    if not values:
        return None
    total = Decimal(sum(values))
    return int((total / Decimal(len(values))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def effective_order_count(orders: Sequence[Order]) -> int:
    """返回计入消费的订单数（整单退款订单不计入）。

    参数:
        orders: 订单序列。

    返回:
        有效订单数。
    """
    return sum(1 for o in orders if o.counted_in_spend)


def describe_milestone_criteria() -> List[Dict[str, str]]:
    """返回里程碑口径说明（单一事实来源，供 README 与 demo 引用）。

    返回:
        列表，每项 ``{"title": ..., "criteria": ...}``。
    """
    return [
        {"title": "第一次点单", "criteria": "窗口内时间最早的一单；已退款也算点过，但会标注退款状态。"},
        {"title": "最长连续天数", "criteria": "日历天连续有有效消费的最长天数；整单退款订单不计入，同日多单只算一天。"},
        {"title": "单笔最大金额", "criteria": "计入消费订单里净额最大的一单；并列取更早的一单。"},
        {
            "title": "最贵的深夜订单",
            "criteria": "下单时间落在 %02d:00–%02d:59 的消费订单里净额最大的一单；并列取更早的一单。"
            % (SLOT_NIGHT_START_HOUR, (SLOT_NIGHT_END_HOUR - 1) % 24),
        },
        {"title": "累计消费", "criteria": "净额之和（实付 − 退款）；实付总额与退款总额同时给出，便于核对。"},
        {"title": "最常点餐品", "criteria": "数量最多；并列依次比较出现订单数、首次出现时间、名称字典序。"},
    ]
