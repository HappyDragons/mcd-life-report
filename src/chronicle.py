"""订单序列 → 人生分期（时间轴编年史）。

本模块把「最近 N 单」看成一段可以被讲述的关系史，并把它切成五个阶段：

============ ============================================================
阶段          判据（全部为确定性规则，阈值均以常量给出并附设计理由）
============ ============================================================
初遇期        第 1 单；以及距首单 ≤ ``FIRST_ENCOUNTER_DAYS``（14 天）的订单。
探索期        该单引入了此前从未出现的**新品类**（没有品类字段时降级为
              「新商品名首次出现」，降级情况会在 ``warnings`` 里明说）。
稳定期        该单没有引入新品类，且未触发回潮 / 倦怠判据。
倦怠期        ① 断档 ≥ ``GAP_DORMANT_DAYS``（30 天）且**此后不再下单**；
              ② 间隔 ≥ ``GAP_FADE_DAYS``（14 天）且 ≥ 自身中位节奏的
                 ``FADE_MEDIAN_MULTIPLE``（2 倍）；
              ③ 连续 ``DECLINE_STREAK``（3）单实付金额递减且低于中位客单价。
回潮期        断档 ≥ ``GAP_DORMANT_DAYS``（30 天）后又回来了（后面还有订单）。
============ ============================================================

阈值设计理由（不是随便挑的数字）：

- **14 天**：半个月是「关系还在热恋」的自然观察窗，也是麦当劳优惠券与活动的常见
  周转周期，两周内连点两次基本可以认定用户处在新鲜感阶段。
- **30 天**：一个自然月。跨越一个完整月度周期没有下单，在消费品类的经验里已经是
  「关系冷却」而不是「今天没胃口」。这是把"断档"与"正常波动"分开的最小自然单位。
- **2 倍中位节奏**：绝对阈值无法适配所有人（有人周更、有人月更）。所以引入
  「跟用户自己的节奏比」的相对判据，配合 14 天的绝对下限，避免高频用户
  （中位间隔 2 天）因为隔了 5 天就被误判成倦怠。
- **连续 3 单递减**：单个 5 块钱的波动说明不了问题，连续三单金额走低且低于自己的
  中位客单价，才构成"投入在收缩"的确定性证据。

本模块 **不使用随机数、不调用大模型**，同样的输入永远得到同样的分期结果。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .mcd_adapter import ORDER_LIST_WINDOW, Order, format_yuan

# --------------------------------------------------------------------------- #
# 分期常量（改这些值就是在改判据，请同步更新上面的判据表与 README）
# --------------------------------------------------------------------------- #

PHASE_FIRST = "初遇期"
PHASE_EXPLORE = "探索期"
PHASE_STABLE = "稳定期"
PHASE_BURNOUT = "倦怠期"
PHASE_REVIVAL = "回潮期"
PHASE_UNKNOWN = "未分期"

#: 五个正式阶段（``PHASE_UNKNOWN`` 是数据缺失的兜底，不属于人生阶段）。
PHASE_ORDER: Tuple[str, ...] = (PHASE_FIRST, PHASE_EXPLORE, PHASE_STABLE, PHASE_BURNOUT, PHASE_REVIVAL)

#: 初遇期窗口：距首单多少天以内仍算「蜜月期」。
FIRST_ENCOUNTER_DAYS = 14.0

#: 断档阈值：超过一个自然月没有下单，关系视为冷却。
GAP_DORMANT_DAYS = 30.0

#: 相对降速判据的绝对下限（天）与相对倍数。
GAP_FADE_DAYS = 14.0
FADE_MEDIAN_MULTIPLE = 2.0

#: 金额走弱判据需要的连续递减单数。
DECLINE_STREAK = 3

#: 时段划分（左闭右开，跨零点用 ``night_end`` 表示）。
TIME_SLOTS: Tuple[str, ...] = ("早晨", "午市", "下午茶", "晚市", "深夜")
SLOT_MORNING_START_HOUR = 6
SLOT_LUNCH_START_HOUR = 11
SLOT_AFTERNOON_START_HOUR = 14
SLOT_DINNER_START_HOUR = 17
SLOT_NIGHT_START_HOUR = 22
SLOT_NIGHT_END_HOUR = 5


class ChronicleError(ValueError):
    """分期输入不合法（例如订单未带时间字段）时抛出。"""


def time_slot(moment: datetime) -> str:
    """判断某个时刻属于哪个时段。

    参数:
        moment: 北京时间（naive）。

    返回:
        ``"早晨"``（06:00–10:59）/ ``"午市"``（11:00–13:59）/ ``"下午茶"``（14:00–16:59）/
        ``"晚市"``（17:00–21:59）/ ``"深夜"``（22:00–04:59）。
    """
    hour = moment.hour
    if SLOT_NIGHT_START_HOUR <= hour <= 23:
        return "深夜"
    if 0 <= hour < SLOT_NIGHT_END_HOUR:
        return "深夜"
    if SLOT_MORNING_START_HOUR <= hour < SLOT_LUNCH_START_HOUR:
        return "早晨"
    if SLOT_LUNCH_START_HOUR <= hour < SLOT_AFTERNOON_START_HOUR:
        return "午市"
    if SLOT_AFTERNOON_START_HOUR <= hour < SLOT_DINNER_START_HOUR:
        return "下午茶"
    if SLOT_DINNER_START_HOUR <= hour < SLOT_NIGHT_START_HOUR:
        return "晚市"
    # 05:00–05:59：黎明前，归入早晨
    return "早晨"


def is_night_order(moment: datetime, night_start_hour: int = SLOT_NIGHT_START_HOUR, night_end_hour: int = SLOT_NIGHT_END_HOUR) -> bool:
    """判断某时刻是否属于「深夜」（默认 22:00–04:59，跨零点）。

    参数:
        moment: 北京时间（naive）。
        night_start_hour: 深夜起点小时（含）。
        night_end_hour: 深夜终点小时（不含，跨零点）。

    返回:
        是否深夜。
    """
    hour = moment.hour
    if night_start_hour > night_end_hour:
        return hour >= night_start_hour or hour < night_end_hour
    return night_start_hour <= hour < night_end_hour


def describe_phase_criteria() -> List[Dict[str, str]]:
    """返回五个阶段的判据说明，供文档、demo 输出与 README 引用（单一事实来源）。

    返回:
        列表，每项形如 ``{"phase": "倦怠期", "criteria": "...", "why": "..."}``。
    """
    return [
        {
            "phase": PHASE_FIRST,
            "criteria": "第 1 单，以及距首单 ≤ %.0f 天的订单。" % FIRST_ENCOUNTER_DAYS,
            "why": "半个月是新鲜感的自然观察窗，也是活动与优惠券的常见周转周期。",
        },
        {
            "phase": PHASE_EXPLORE,
            "criteria": "该单带来此前从未出现的新品类（无品类字段时降级为「新商品名首次出现」）。",
            "why": "尝新就是探索；引入新品类的时刻比金额更能标记探索行为。",
        },
        {
            "phase": PHASE_STABLE,
            "criteria": "没有引入新品类，且未触发回潮 / 倦怠判据。",
            "why": "口味收敛、节奏平稳，是关系进入日常的确定信号。",
        },
        {
            "phase": PHASE_BURNOUT,
            "criteria": (
                "① 断档 ≥ %.0f 天且此后不再下单；或 ② 间隔 ≥ %.0f 天且 ≥ 自身中位节奏的 %.1f 倍；"
                "或 ③ 连续 %d 单实付递减且低于中位客单价。" % (GAP_DORMANT_DAYS, GAP_FADE_DAYS, FADE_MEDIAN_MULTIPLE, DECLINE_STREAK)
            ),
            "why": "绝对断档看自然月，相对降速看用户自己的节奏，金额收缩看投入意愿，三条互为补充。",
        },
        {
            "phase": PHASE_REVIVAL,
            "criteria": "断档 ≥ %.0f 天后又回来了（该单之后仍有订单）。" % GAP_DORMANT_DAYS,
            "why": "冷却后又回来，是比连续消费更值得标注的行为转折。",
        },
    ]


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #


@dataclass
class PhaseSegment:
    """一个连续的人生阶段片段。"""

    phase: str
    """阶段名。"""

    start_index: int
    """在输入订单序列（升序）中的起始下标，0-based。"""

    end_index: int
    """结束下标（含）。"""

    start_at: Optional[datetime]
    """片段首单时间；可能为 ``None``（该段全部缺时间）。"""

    end_at: Optional[datetime]
    """片段末单时间；可能为 ``None``。"""

    order_count: int
    """本段订单数。"""

    spend_cents: int
    """本段净消费（分），整单退款订单不计入。"""

    span_days: float
    """本段首末单相隔天数（同一天为 0）。"""

    order_ids: List[str] = field(default_factory=list)
    """本段订单号，便于核对。"""

    reasons: List[str] = field(default_factory=list)
    """本段每一单的判据说明。"""

    @property
    def label(self) -> str:
        """人类可读的区间标签，例如 ``"2026-05-04 ~ 2026-05-11"``。"""
        start = self.start_at.strftime("%Y-%m-%d") if self.start_at else "时间未核实"
        end = self.end_at.strftime("%Y-%m-%d") if self.end_at else "时间未核实"
        return start if start == end else "%s ~ %s" % (start, end)


@dataclass
class TurnPoint:
    """人生转折点：时间轴上被显式标注的时刻。"""

    index: int
    """在订单序列中的下标，0-based。"""

    order_id: str
    """订单号。"""

    at: Optional[datetime]
    """发生时间。"""

    reasons: List[str] = field(default_factory=list)
    """触发原因（可能多条，例如既是最大金额又是最贵深夜单）。"""


@dataclass
class Chronicle:
    """一次分期的完整结果。"""

    phases: List[str] = field(default_factory=list)
    """与输入订单一一对应的阶段名。"""

    reasons: List[str] = field(default_factory=list)
    """与输入订单一一对应的判据说明。"""

    segments: List[PhaseSegment] = field(default_factory=list)
    """合并同类相邻阶段后的片段列表。"""

    gaps_days: List[Optional[float]] = field(default_factory=list)
    """与输入订单一一对应的「距上一单天数」，首单为 ``None``。"""

    median_gap_days: Optional[float] = None
    """窗口内间隔的中位数（天）；不足两单时为 ``None``。"""

    median_amount_cents: Optional[int] = None
    """计入消费的订单的净额中位数（分）。"""

    category_signal: str = "none"
    """探索期判据依赖的信号源：``"category"`` / ``"item_name"`` / ``"none"``。"""

    warnings: List[str] = field(default_factory=list)
    """数据质量与口径提示，必须原样展示给用户。"""

    turn_points: List[TurnPoint] = field(default_factory=list)
    """人生转折点。"""

    def phase_counts(self) -> Dict[str, int]:
        """统计每个阶段各有多少单。"""
        counts: Dict[str, int] = {}
        for phase in self.phases:
            counts[phase] = counts.get(phase, 0) + 1
        return counts


# --------------------------------------------------------------------------- #
# 分期主逻辑
# --------------------------------------------------------------------------- #


def _median(values: Sequence[float]) -> Optional[float]:
    """安全中位数：空序列返回 ``None``。"""
    if not values:
        return None
    return float(statistics.median(values))


def classify_phases(orders: Sequence[Order]) -> Chronicle:
    """把订单序列切成人生五阶段。

    参数:
        orders: 订单序列，要求**按时间升序**（``mcd_adapter.adapt_orders`` 的返回值已满足）。
            含 ``created_at`` 为 ``None`` 的订单时，它们会被标为 ``"未分期"`` 并排在最后。

    返回:
        :class:`Chronicle`。

    异常:
        ChronicleError: ``orders`` 为空时抛出（没有订单就没有人生史，不应伪造）。

    说明:
        判据全部落在常量上（见模块 docstring 的表格），顺序即优先级：
        初遇 → 回潮 → 倦怠 → 探索 → 稳定，第一个命中的判据生效。
    """
    if not orders:
        raise ChronicleError("订单序列为空，无法分期。")

    timed: List[Order] = [o for o in orders if o.created_at is not None]
    chronicle = Chronicle()
    chronicle.phases = [PHASE_UNKNOWN] * len(orders)
    chronicle.reasons = ["缺少下单时间，无法分期"] * len(orders)
    chronicle.gaps_days = [None] * len(orders)

    if not timed:
        chronicle.warnings.append("所有订单都缺少可解析的下单时间，本次无法产出任何分期结论。")
        return chronicle

    # 只有带时间的订单参与分期，但下标要映射回原始序列。
    index_of: List[int] = []
    for position, order in enumerate(orders):
        if order.created_at is not None:
            index_of.append(position)

    n = len(timed)
    timed_gaps: List[Optional[float]] = [None] * n
    for i in range(1, n):
        delta = timed[i].created_at - timed[i - 1].created_at  # type: ignore[operator]
        timed_gaps[i] = delta.total_seconds() / 86400.0

    median_gap = _median([g for g in timed_gaps[1:] if g is not None])
    counted_nets = [o.net_cents for o in timed if o.counted_in_spend]
    median_amount = _median([float(v) for v in counted_nets])
    chronicle.median_gap_days = median_gap
    chronicle.median_amount_cents = int(median_amount) if median_amount is not None else None

    # 判断探索期依赖的信号源：品类优先，其次商品名。
    has_category = any(item.category != "未分类" for o in timed for item in o.items)
    has_name = any(item.name != "未标注商品" for o in timed for item in o.items)
    if has_category:
        chronicle.category_signal = "category"
    elif has_name:
        chronicle.category_signal = "item_name"
        chronicle.warnings.append(
            "订单数据缺少商品品类字段，「探索期」已降级为按「商品名首次出现」判定（口径不同，结果可能偏宽松）。"
        )
    else:
        chronicle.category_signal = "none"
        chronicle.warnings.append("订单数据既无品类也无商品名，「探索期」判据无法生效，本期不会出现探索期。")

    seen_categories: List[str] = []
    seen_names: List[str] = []
    first_at = timed[0].created_at

    for i, order in enumerate(timed):
        slot_index = index_of[i]
        gap = timed_gaps[i]
        elapsed = (order.created_at - first_at).total_seconds() / 86400.0  # type: ignore[operator]

        phase, reason = _classify_one(
            i=i,
            total=n,
            elapsed_days=elapsed,
            gap_days=gap,
            median_gap=median_gap,
            median_amount=median_amount,
            timed=timed,
            seen_categories=seen_categories,
            seen_names=seen_names,
            category_signal=chronicle.category_signal,
        )
        chronicle.phases[slot_index] = phase
        chronicle.reasons[slot_index] = reason
        chronicle.gaps_days[slot_index] = gap

        for category in order.categories:
            if category not in seen_categories:
                seen_categories.append(category)
        for name in order.item_names:
            if name not in seen_names:
                seen_names.append(name)

    # 时间无法解析的订单，追加一条总体提示。
    untimed = len(orders) - len(timed)
    if untimed:
        chronicle.warnings.append("有 %d 单缺少可解析的下单时间，已标为「未分期」并排在时间轴末端。" % untimed)

    chronicle.segments = _build_segments(orders, chronicle)

    if len(orders) >= ORDER_LIST_WINDOW:
        chronicle.warnings.append(
            "MCP 的 order-list 无分页参数，只返回最近 %d 单：本时间轴是「最近一段」，不是完整消费史。"
            % ORDER_LIST_WINDOW
        )
    if n <= 2:
        chronicle.warnings.append("窗口内只有 %d 单，分期结果样本过小，仅供参考，不具有统计意义。" % n)

    return chronicle


def _classify_one(
    i: int,
    total: int,
    elapsed_days: float,
    gap_days: Optional[float],
    median_gap: Optional[float],
    median_amount: Optional[float],
    timed: Sequence[Order],
    seen_categories: Sequence[str],
    seen_names: Sequence[str],
    category_signal: str,
) -> Tuple[str, str]:
    """对第 ``i`` 单（0-based，仅含带时间的订单）执行判据，返回 ``(阶段, 判据说明)``。

    参数:
        i: 在带时间订单序列中的下标。
        total: 带时间订单总数。
        elapsed_days: 距首单天数。
        gap_days: 距上一单天数（首单为 ``None``）。
        median_gap: 全窗口间隔中位数；不足两单为 ``None``。
        median_amount: 窗口净额中位数；无有效金额为 ``None``。
        timed: 带时间的订单序列。
        seen_categories: 在本单**之前**出现过的品类。
        seen_names: 在本单**之前**出现过的商品名。
        category_signal: 探索期信号源。

    返回:
        ``(阶段名, 中文判据说明)``。
    """
    if i == 0:
        return PHASE_FIRST, "全部故事的第 1 单，一切从这里开始。"

    if elapsed_days <= FIRST_ENCOUNTER_DAYS:
        return PHASE_FIRST, "距首单 %.1f 天（≤ %.0f 天），仍在初遇期。" % (elapsed_days, FIRST_ENCOUNTER_DAYS)

    # ① 回潮：断档 ≥ 30 天后又回来了
    if gap_days is not None and gap_days >= GAP_DORMANT_DAYS and i < total - 1:
        return PHASE_REVIVAL, "距上一单 %.1f 天（≥ %.0f 天），断档一个自然月后又回来了。" % (gap_days, GAP_DORMANT_DAYS)

    # ② 倦怠 a：断档 ≥ 30 天且此后未再下单
    if gap_days is not None and gap_days >= GAP_DORMANT_DAYS and i == total - 1:
        return PHASE_BURNOUT, "距上一单 %.1f 天（≥ %.0f 天），且这是目前最后一单，热度正在冷却。" % (gap_days, GAP_DORMANT_DAYS)

    # ③ 倦怠 b：相对自身节奏明显降速
    if (
        gap_days is not None
        and median_gap is not None
        and gap_days >= GAP_FADE_DAYS
        and gap_days >= FADE_MEDIAN_MULTIPLE * median_gap
    ):
        return PHASE_BURNOUT, "距上一单 %.1f 天，达到自身中位节奏（%.1f 天）的 %.1f 倍。" % (
            gap_days,
            median_gap,
            gap_days / median_gap if median_gap else 0.0,
        )

    # ④ 倦怠 c：连续 N 单金额递减且低于中位客单价
    if i >= DECLINE_STREAK - 1 and median_amount is not None:
        window = timed[i - DECLINE_STREAK + 1 : i + 1]
        if all(o.counted_in_spend for o in window):
            nets = [o.net_cents for o in window]
            strictly_down = all(nets[k] < nets[k - 1] for k in range(1, len(nets)))
            if strictly_down and nets[-1] < median_amount:
                return PHASE_BURNOUT, "连续 %d 单金额递减（%s），且低于中位客单价 %s。" % (
                    DECLINE_STREAK,
                    " → ".join(format_yuan(v) for v in nets),
                    format_yuan(int(median_amount)),
                )

    # ⑤ 探索：出现新品类 / 新商品
    order = timed[i]
    if category_signal == "category":
        fresh = [c for c in order.categories if c not in seen_categories]
        if fresh:
            return PHASE_EXPLORE, "首次出现新品类：%s。" % "、".join(fresh)
    elif category_signal == "item_name":
        fresh = [c for c in order.item_names if c not in seen_names]
        if fresh:
            return PHASE_EXPLORE, "首次出现新商品：%s。" % "、".join(fresh)
    else:
        return PHASE_STABLE, "数据缺少品类与商品名，无法判断是否尝新，按稳定期处理。"

    # ⑥ 稳定
    detail = "沿用已有品类"
    if order.categories:
        detail = "仍在 %s 里打转" % "、".join(order.categories)
    return PHASE_STABLE, "%s，没有引入新品类。" % detail


def _build_segments(orders: Sequence[Order], chronicle: Chronicle) -> List[PhaseSegment]:
    """把逐单阶段合并成连续的阶段片段。

    参数:
        orders: 原始订单序列（与 ``chronicle.phases`` 对齐）。
        chronicle: 已填好 ``phases`` 的分期结果。

    返回:
        :class:`PhaseSegment` 列表，按时间顺序。
    """
    segments: List[PhaseSegment] = []
    if not chronicle.phases:
        return segments

    start = 0
    for i in range(1, len(orders) + 1):
        is_last = i == len(orders)
        if is_last or chronicle.phases[i] != chronicle.phases[start]:
            chunk = list(range(start, i))
            chunk_orders = [orders[k] for k in chunk]
            timed_chunk = [o for o in chunk_orders if o.created_at is not None]
            start_at = timed_chunk[0].created_at if timed_chunk else None
            end_at = timed_chunk[-1].created_at if timed_chunk else None
            span = 0.0
            if start_at is not None and end_at is not None:
                span = (end_at - start_at).total_seconds() / 86400.0
            segments.append(
                PhaseSegment(
                    phase=chronicle.phases[start],
                    start_index=start,
                    end_index=i - 1,
                    start_at=start_at,
                    end_at=end_at,
                    order_count=len(chunk_orders),
                    spend_cents=sum(o.net_cents for o in chunk_orders if o.counted_in_spend),
                    span_days=span,
                    order_ids=[o.order_id for o in chunk_orders],
                    reasons=[chronicle.reasons[k] for k in chunk],
                )
            )
            start = i

    return segments


# --------------------------------------------------------------------------- #
# 转折点标注
# --------------------------------------------------------------------------- #


def compute_turn_points(
    orders: Sequence[Order],
    chronicle: Chronicle,
    milestone_map: Optional[Dict[str, Any]] = None,
) -> List[TurnPoint]:
    """标出时间轴上的「人生转折点」。

    参数:
        orders: 订单序列。
        chronicle: 分期结果。
        milestone_map: ``milestones.extract_milestones`` 的结果，用于取「单笔最大金额」
            与「最贵的深夜订单」。为 ``None`` 时只产出分期类转折点。

    返回:
        按时间排序的 :class:`TurnPoint` 列表。

    说明:
        转折点判据是确定性的，共三类：

        1. **进入回潮期或倦怠期的首单**——关系状态发生实质变化的那一单；
        2. **单笔最大金额订单**——投入最重的一次；
        3. **最贵的深夜订单**——最脆弱（或最放肆）的一次。
    """
    by_index: Dict[int, List[str]] = {}

    for i in range(len(orders)):
        phase = chronicle.phases[i] if i < len(chronicle.phases) else PHASE_UNKNOWN
        previous = chronicle.phases[i - 1] if i > 0 else None
        if phase in (PHASE_REVIVAL, PHASE_BURNOUT) and phase != previous:
            gap = chronicle.gaps_days[i] if i < len(chronicle.gaps_days) else None
            gap_text = "%.1f 天" % gap if gap is not None else "未知天数"
            verb = "归来" if phase == PHASE_REVIVAL else "冷却"
            by_index.setdefault(i, []).append("断档 %s后%s，进入%s" % (gap_text, verb, phase))

    if milestone_map:
        biggest = milestone_map.get("biggest_order")
        if biggest is not None and biggest.value is not None and biggest.order_index is not None:
            by_index.setdefault(biggest.order_index, []).append("全窗口单笔最大金额 %s" % biggest.display)

        priciest_night = milestone_map.get("priciest_night_order")
        if (
            priciest_night is not None
            and priciest_night.value is not None
            and priciest_night.order_index is not None
        ):
            by_index.setdefault(priciest_night.order_index, []).append("最贵的深夜订单 %s" % priciest_night.display)

    points: List[TurnPoint] = []
    for index in sorted(by_index):
        order = orders[index]
        points.append(
            TurnPoint(
                index=index,
                order_id=order.order_id,
                at=order.created_at,
                reasons=by_index[index],
            )
        )
    return points


# --------------------------------------------------------------------------- #
# 时间轴模型（给 card.py 用）
# --------------------------------------------------------------------------- #


def build_timeline(
    orders: Sequence[Order],
    chronicle: Chronicle,
) -> List[Dict[str, Any]]:
    """构建时间轴的渲染模型。

    参数:
        orders: 订单序列（升序）。
        chronicle: 分期结果（其 ``turn_points`` 应已填充）。

    返回:
        与订单一一对应的字典列表，字段：
        ``index`` / ``order_id`` / ``at`` / ``day_offset`` / ``amount_cents`` /
        ``net_cents`` / ``slot`` / ``phase`` / ``phase_reason`` / ``refunded`` /
        ``is_origin`` / ``is_turn_point`` / ``turn_reasons`` / ``top_items`` / ``quantity``。

    说明:
        气泡面积 ∝ 净消费金额，颜色 ∝ 时段，横轴 ∝ 时间，转折点被显式标注。
        ``day_offset`` 的首单天然为 0；时间缺失的订单 ``day_offset`` 为 ``None``。
    """
    turn_lookup: Dict[int, List[str]] = {p.index: list(p.reasons) for p in chronicle.turn_points}
    timeline: List[Dict[str, Any]] = []

    origin = None
    for order in orders:
        if order.created_at is not None:
            origin = order.created_at
            break

    for index, order in enumerate(orders):
        offset: Optional[float] = None
        if order.created_at is not None and origin is not None:
            offset = (order.created_at - origin).total_seconds() / 86400.0

        items_sorted = sorted(order.items, key=lambda it: (-it.quantity, it.name))
        top_items = "、".join("%s×%d" % (it.name, it.quantity) for it in items_sorted[:2]) or "商品明细缺失"

        timeline.append(
            {
                "index": index,
                "order_id": order.order_id,
                "at": order.created_at.isoformat(sep=" ") if order.created_at else None,
                "day_offset": offset,
                "amount_cents": order.amount_cents,
                "net_cents": order.net_cents,
                "slot": time_slot(order.created_at) if order.created_at else "时段未核实",
                "phase": chronicle.phases[index] if index < len(chronicle.phases) else PHASE_UNKNOWN,
                "phase_reason": chronicle.reasons[index] if index < len(chronicle.reasons) else "",
                "refunded": order.is_fully_refunded,
                "is_origin": index == 0,
                "is_turn_point": index in turn_lookup,
                "turn_reasons": turn_lookup.get(index, []),
                "top_items": top_items,
                "quantity": order.total_quantity,
            }
        )

    return timeline


def summarise_phase_flow(chronicle: Chronicle) -> str:
    """把分期结果压成一行流程字符串，例如 ``"初遇期 → 探索期 → 稳定期 → 回潮期 → 倦怠期"``。

    参数:
        chronicle: 分期结果。

    返回:
        流程字符串；无片段时返回 ``"未分期"``。
    """
    if not chronicle.segments:
        return PHASE_UNKNOWN
    seen: List[str] = []
    for segment in chronicle.segments:
        if segment.phase not in seen:
            seen.append(segment.phase)
    return " → ".join(seen)


def window_span_days(orders: Sequence[Order]) -> Optional[float]:
    """计算窗口内首末单相隔天数。

    参数:
        orders: 订单序列。

    返回:
        天数；少于两单或时间缺失时返回 ``None``。
    """
    timed = [o.created_at for o in orders if o.created_at is not None]
    if len(timed) < 2:
        return None
    return (timed[-1] - timed[0]).total_seconds() / 86400.0


def timedelta_days(days: float) -> timedelta:
    """把天数转成 ``timedelta``（小工具，供测试与展示使用）。

    参数:
        days: 天数。

    返回:
        对应的 ``timedelta``。
    """
    return timedelta(days=days)
