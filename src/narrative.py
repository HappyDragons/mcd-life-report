"""「人生旁白」：用确定性规则从真实数据里推出叙述文本。

**本模块的硬约束（也是它存在的意义）**

1. 不 ``import random``，不用任何随机数。
2. 不调用大模型，不调用任何网络接口。
3. 每一句话都必须能**回溯到具体数据**：文本里的数字全部来自订单，
   阈值全部是模块级常量，且在每个判据旁注明理由。
4. 数据缺失时输出「未核实 / 数据不足」，而不是编一个听起来很像真的故事。

因此同一份订单输入，无论跑多少次、在哪台机器上跑，都会得到**逐字相同**的旁白。

维度说明（每个维度都是「先算事实，再套阈值」）：

=========== ============================================================
维度          判据
=========== ============================================================
主时段        计入消费的订单按时段计数，取最多的那个时段；占比 ≥ 40% 才算
             「主时段」，否则归为「时段分散型」（时段平局时按
             早晨→午市→下午茶→晚市→深夜的固定顺序取胜）。
夜食指数      深夜（22:00–04:59）订单占比 ≥ 30% 记为「夜行倾向」，并始终
             给出真实分子分母。
单品依赖度    最常点餐品的数量占全部商品件数的比例：≥ 30% 为「重度依赖型」，
             ≥ 15% 为「稳定复购型」，否则为「广泛探索型」。
消费走势      把计入消费的订单按时间对半切，比较前后半段平均净额；
             变化超过 ±15% 记为「上升 / 下降」，否则「平稳」。
=========== ============================================================
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from . import chronicle as chronicle_mod
from .chronicle import (
    PHASE_BURNOUT,
    PHASE_EXPLORE,
    PHASE_FIRST,
    PHASE_REVIVAL,
    PHASE_STABLE,
    PHASE_UNKNOWN,
    TIME_SLOTS,
    Chronicle,
)
from .mcd_adapter import ORDER_LIST_WINDOW, Order, format_yuan

# --------------------------------------------------------------------------- #
# 文本常量与阈值
# --------------------------------------------------------------------------- #

#: 主时段判定的最低占比（低于此值算「时段分散型」）。
DOMINANT_SLOT_RATIO = 0.40

#: 夜行倾向的最低深夜占比。
NIGHT_OWL_RATIO = 0.30

#: 单品依赖度阈值。
HEAVY_ITEM_RATIO = 0.30
REPEAT_ITEM_RATIO = 0.15

#: 消费走势的判定带宽（相对变化绝对值）。
TREND_BAND = 0.15

#: 各阶段的一句话性格描述（确定性文案，不含任何变量）。
PHASE_SENTENCE: Dict[str, str] = {
    PHASE_FIRST: "关系刚开始，一切都很新鲜",
    PHASE_EXPLORE: "你开始点菜单上没碰过的东西",
    PHASE_STABLE: "口味定下来了，点单变成肌肉记忆",
    PHASE_BURNOUT: "热度在退，间隔被拉长",
    PHASE_REVIVAL: "隔了很久，你又回来了",
    PHASE_UNKNOWN: "这几单缺少可用时间，无法归入任何阶段",
}

#: 时段顺序固定，用于平局决胜（保证结果唯一可复现）。
_SLOT_TIEBREAK = {name: index for index, name in enumerate(TIME_SLOTS)}

_HONESTY_WINDOW = (
    "本报告的全部结论只来自 MCP `order-list` 返回的最近 %d 单（该接口无分页参数）。"
    "所有「累计 / 最长 / 最贵」都是这 %d 单窗口内的数值，**不等于人生总和**。" % (ORDER_LIST_WINDOW, ORDER_LIST_WINDOW)
)


class NarrativeError(ValueError):
    """构建旁白所需数据不足时抛出。"""


# --------------------------------------------------------------------------- #
# 事实计算
# --------------------------------------------------------------------------- #


def slot_distribution(orders: Sequence[Order]) -> Dict[str, Any]:
    """统计计入消费订单的时段分布。

    参数:
        orders: 订单序列。

    返回:
        字典，含 ``counts``（各时段单数）、``total``（有时间的有效订单数）、
        ``dominant``（主时段或 ``"时段分散型"``）、``dominant_ratio``、
        ``night_count``、``night_ratio``、``has_night_bias``。

    说明:
        未退款但缺少时间的订单不计入分母（无法判断时段），并会在 ``skipped`` 中计数。
    """
    counts = {name: 0 for name in TIME_SLOTS}
    skipped = 0
    for order in orders:
        if not order.counted_in_spend:
            continue
        if order.created_at is None:
            skipped += 1
            continue
        counts[chronicle_mod.time_slot(order.created_at)] += 1

    total = sum(counts.values())
    if total == 0:
        return {
            "counts": counts,
            "total": 0,
            "skipped": skipped,
            "dominant": "时段未核实",
            "dominant_ratio": None,
            "night_count": 0,
            "night_ratio": None,
            "has_night_bias": False,
        }

    dominant = max(TIME_SLOTS, key=lambda name: (counts[name], -_SLOT_TIEBREAK[name]))
    dominant_ratio = counts[dominant] / float(total)
    night_count = counts["深夜"]
    night_ratio = night_count / float(total)

    return {
        "counts": counts,
        "total": total,
        "skipped": skipped,
        "dominant": dominant if dominant_ratio >= DOMINANT_SLOT_RATIO else "时段分散型",
        "dominant_ratio": dominant_ratio,
        "night_count": night_count,
        "night_ratio": night_ratio,
        "has_night_bias": night_ratio >= NIGHT_OWL_RATIO,
    }


def item_dependency(orders: Sequence[Order], top_item_name: Optional[str]) -> Dict[str, Any]:
    """计算单品依赖度。

    参数:
        orders: 订单序列。
        top_item_name: 最常点餐品名称（来自 ``milestones``）；``None`` 表示不存在。

    返回:
        字典，含 ``top_name``、``top_quantity``、``total_quantity``、``ratio``、``level``。
    """
    total_quantity = 0
    top_quantity = 0
    for order in orders:
        if not order.counted_in_spend:
            continue
        for item in order.items:
            total_quantity += item.quantity
            if top_item_name is not None and item.name == top_item_name:
                top_quantity += item.quantity

    if total_quantity == 0 or top_item_name is None:
        return {
            "top_name": top_item_name,
            "top_quantity": top_quantity,
            "total_quantity": total_quantity,
            "ratio": None,
            "level": "数据不足",
        }

    ratio = top_quantity / float(total_quantity)
    if ratio >= HEAVY_ITEM_RATIO:
        level = "重度依赖型"
    elif ratio >= REPEAT_ITEM_RATIO:
        level = "稳定复购型"
    else:
        level = "广泛探索型"

    return {
        "top_name": top_item_name,
        "top_quantity": top_quantity,
        "total_quantity": total_quantity,
        "ratio": ratio,
        "level": level,
    }


def spend_trend(orders: Sequence[Order]) -> Dict[str, Any]:
    """比较前后半段平均净额，判断消费走势。

    参数:
        orders: 订单序列。

    返回:
        字典，含 ``level``（``"上升"`` / ``"下降"`` / ``"平稳"`` / ``"数据不足"``）、
        ``first_half_avg_cents``、``second_half_avg_cents``、``change_ratio``、``split``。

    说明:
        只统计计入消费且时间已核实的订单，按时间顺序对半切；奇数单时前半段少一个。
    """
    valid = [o for o in orders if o.counted_in_spend and o.created_at is not None]
    if len(valid) < 2:
        return {
            "level": "数据不足",
            "first_half_avg_cents": None,
            "second_half_avg_cents": None,
            "change_ratio": None,
            "split": len(valid),
        }

    split = len(valid) // 2
    first_half = valid[:split]
    second_half = valid[split:]
    first_avg = sum(o.net_cents for o in first_half) / float(len(first_half))
    second_avg = sum(o.net_cents for o in second_half) / float(len(second_half))

    if first_avg <= 0:
        return {
            "level": "平稳",
            "first_half_avg_cents": int(round(first_avg)),
            "second_half_avg_cents": int(round(second_avg)),
            "change_ratio": None,
            "split": split,
        }

    change = (second_avg - first_avg) / first_avg
    if change > TREND_BAND:
        level = "上升"
    elif change < -TREND_BAND:
        level = "下降"
    else:
        level = "平稳"

    return {
        "level": level,
        "first_half_avg_cents": int(round(first_avg)),
        "second_half_avg_cents": int(round(second_avg)),
        "change_ratio": change,
        "split": split,
    }


# --------------------------------------------------------------------------- #
# 旁白组装
# --------------------------------------------------------------------------- #


def _segment_sentences(chronicle: Chronicle) -> List[str]:
    """把分期片段按阶段聚合后转成叙述句（同一阶段出现多次会合并计数）。

    参数:
        chronicle: 分期结果。

    返回:
        每个出现过的人生阶段一句，例如
        ``"初遇期（2 单，¥63.50）：关系刚开始，一切都很新鲜。"``。
    """
    aggregated: Dict[str, Dict[str, Any]] = {}
    for segment in chronicle.segments:
        entry = aggregated.setdefault(segment.phase, {"orders": 0, "spend": 0, "labels": []})
        entry["orders"] += segment.order_count
        entry["spend"] += segment.spend_cents
        entry["labels"].append(segment.label)

    sentences: List[str] = []
    for phase, entry in aggregated.items():
        sentences.append(
            "%s %d 单 %s：%s。"
            % (
                phase,
                entry["orders"],
                format_yuan(entry["spend"]),
                PHASE_SENTENCE.get(phase, "这一段没有可用的阶段说明"),
            )
        )
    return sentences


def _relationship_verdict(chronicle: Chronicle) -> str:
    """根据是否出现回潮 / 倦怠，给出关系判决句。"""
    has_burnout = PHASE_BURNOUT in chronicle.phases
    has_revival = PHASE_REVIVAL in chronicle.phases

    if has_burnout and has_revival:
        return "你离开过，也回来过——这是长期关系才有的形状。"
    if has_burnout and not has_revival:
        return "最后一次间隔明显拉长，关系正在冷却：这不是结束，但也不是热恋。"
    if has_revival and not has_burnout:
        return "断档之后你回来了，说明这家店在你心里还有位置。"
    if PHASE_EXPLORE in chronicle.phases:
        return "到目前为止还没有真正的断档，你还在断断续续地尝新。"
    return "到目前为止还没有真正的断档，口味和节奏都很稳。"


def build_narrative(
    orders: Sequence[Order],
    chronicle: Chronicle,
    milestones: Dict[str, Any],
    window_span: Optional[float] = None,
    data_notes: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """生成完整的人生旁白。

    参数:
        orders: 订单序列（升序）。
        chronicle: 分期结果。
        milestones: ``milestones.extract_milestones`` 的返回值（``Dict[str, Milestone]``）。
        window_span: 窗口首末单相隔天数；``None`` 表示无法计算。
        data_notes: 来自适配层的额外诚实性说明（字段缺失等），会原样并入 ``honesty``。

    返回:
        字典，含 ``headline`` / ``paragraphs`` / ``verdict`` / ``tags`` / ``persona`` /
        ``honesty``。所有字段都是确定性的。

    异常:
        NarrativeError: 订单为空时抛出。
    """
    if not orders:
        raise NarrativeError("没有订单，无法生成旁白。")

    counted = [o for o in orders if o.counted_in_spend]
    total_spend = sum(o.net_cents for o in counted)
    span_text = "%.0f 天" % window_span if window_span is not None else "天数未核实"

    slots = slot_distribution(orders)
    top_name = None
    top_milestone = milestones.get("most_ordered_item")
    if top_milestone is not None and top_milestone.available:
        top_name = str(top_milestone.value)
    dependency = item_dependency(orders, top_name)
    trend = spend_trend(orders)

    # ---------------- 第一段：窗口事实 ----------------
    first_order = milestones.get("first_order")
    first_text = first_order.display if first_order is not None and first_order.available else "时间未核实"
    paragraphs: List[str] = [
        "最近 %d 单，你从 %s 吃到 %s，跨过 %s；%d 单计入消费，合计 %s。"
        % (
            len(orders),
            first_text,
            _last_time_text(orders),
            span_text,
            len(counted),
            format_yuan(total_spend),
        )
    ]

    # ---------------- 第二段：分期流水 ----------------
    flow = chronicle_mod.summarise_phase_flow(chronicle)
    segments = _segment_sentences(chronicle)
    paragraphs.append(
        "这段历史被切成 %d 段：%s。%s"
        % (len(chronicle.segments), flow, "".join(segments))
    )

    # ---------------- 第三段：时段 ----------------
    if slots["total"] > 0:
        dominant_text = slots["dominant"]
        ratio_text = "%.0f%%" % (slots["dominant_ratio"] * 100) if slots["dominant_ratio"] is not None else "无法计算"
        night_text = (
            "%d/%d 单落在 22:00–04:59 的深夜（%.0f%%）" % (slots["night_count"], slots["total"], slots["night_ratio"] * 100)
            if slots["night_ratio"] is not None
            else "深夜订单占比无法计算"
        )
        paragraphs.append(
            "时段画像：主时段为 %s（占 %s），%s%s"
            % (
                dominant_text,
                ratio_text,
                night_text,
                "；深夜占比达到夜行线，夜里下单不是偶发。" if slots["has_night_bias"] else "；还没有到「夜行」的程度。",
            )
        )
    else:
        paragraphs.append("时段画像：窗口内没有可用的下单时间，无法判断你是在哪个时段吃东西。")

    # ---------------- 第四段：单品与金额 ----------------
    if dependency["ratio"] is not None:
        item_text = "%s 占了你全部 %d 件商品的 %d 件（%.0f%%）" % (
            dependency["top_name"],
            dependency["total_quantity"],
            dependency["top_quantity"],
            dependency["ratio"] * 100,
        )
    else:
        item_text = "商品明细不足，无法计算单品依赖度"

    if trend["change_ratio"] is not None:
        trend_text = "前后半段平均客单价从 %s 变到 %s（%+.0f%%），走势%s" % (
            format_yuan(trend["first_half_avg_cents"]),
            format_yuan(trend["second_half_avg_cents"]),
            trend["change_ratio"] * 100,
            trend["level"],
        )
    else:
        trend_text = "样本太少，消费走势无法计算"
    paragraphs.append("口味与投入：%s；%s。" % (item_text, trend_text))

    # ---------------- 第五段：关系判决 ----------------
    paragraphs.append(_relationship_verdict(chronicle))

    # ---------------- 判词与标签 ----------------
    tags = [str(slots["dominant"]), str(dependency["level"]), "消费%s" % trend["level"]]
    verdict = "你是「%s · %s · %s」的麦门人：%d 单 / %s / %s。" % (
        tags[0],
        tags[1],
        tags[2],
        len(orders),
        span_text,
        top_milestone.display if top_milestone is not None and top_milestone.available else "招牌未核实",
    )

    headline = "你最近 %d 单，就是一部 %s 的编年史。" % (len(orders), flow)

    # ---------------- 诚实性条款 ----------------
    honesty: List[str] = [_HONESTY_WINDOW]
    refunded_count = sum(1 for o in orders if o.is_fully_refunded)
    if refunded_count:
        honesty.append(
            "窗口内有 %d 单整单退款：它们**不计入任何消费额**（累计消费、单笔最大金额、最贵深夜单、平均客单价都已剔除），"
            "但仍保留在时间轴上并以空心点标记——发生过的事不能被抹掉。" % refunded_count
        )
    if slots["skipped"]:
        honesty.append("有 %d 单缺少可解析的下单时间，未参与时段统计与走势计算。" % slots["skipped"])
    honesty.append(
        "全部旁白由确定性规则从订单数据推出：没有随机数、没有大模型撰写，同一份数据永远得到逐字相同的报告。"
    )
    if data_notes:
        honesty.extend(str(note) for note in data_notes)

    return {
        "headline": headline,
        "paragraphs": paragraphs,
        "verdict": verdict,
        "tags": tags,
        "persona": {"slots": slots, "dependency": dependency, "trend": trend, "flow": flow},
        "honesty": honesty,
    }


def _last_time_text(orders: Sequence[Order]) -> str:
    """取最后一单的展示时间文本。"""
    for order in reversed(list(orders)):
        if order.created_at is not None:
            return order.created_at.strftime("%Y-%m-%d %H:%M")
    return "时间未核实"


def describe_narrative_rules() -> List[Dict[str, str]]:
    """返回旁白规则说明（单一事实来源，供 README 与 demo 引用）。

    返回:
        列表，每项 ``{"dimension": ..., "rule": ...}``。
    """
    return [
        {
            "dimension": "主时段",
            "rule": "计入消费订单按时段计数，占比 ≥ %.0f%% 才指定主时段，否则为「时段分散型」；平局按 %s 顺序取胜。"
            % (DOMINANT_SLOT_RATIO * 100, "→".join(TIME_SLOTS)),
        },
        {
            "dimension": "夜食指数",
            "rule": "深夜（22:00–04:59）订单占比 ≥ %.0f%% 记为夜行倾向，并始终给出真实分子分母。" % (NIGHT_OWL_RATIO * 100),
        },
        {
            "dimension": "单品依赖度",
            "rule": "最常点餐品占全部商品件数比例：≥ %.0f%% 重度依赖型，≥ %.0f%% 稳定复购型，否则广泛探索型。"
            % (HEAVY_ITEM_RATIO * 100, REPEAT_ITEM_RATIO * 100),
        },
        {
            "dimension": "消费走势",
            "rule": "按时间对半切比较平均净额，变化 > +%.0f%% 记上升，< −%.0f%% 记下降，其余为平稳。"
            % (TREND_BAND * 100, TREND_BAND * 100),
        },
    ]
