"""生成 1200×2200 的可分享「人生时间轴长图」。

两条输出路径：

1. **PNG**：依赖 Pillow（**可选依赖**）。Pillow 不是本项目的必需依赖，安装后画质更规整、
   可以直接发朋友圈；同时还需要系统里有一份能渲染中文的字体。
2. **SVG**：**纯标准库**，永远可用。自包含（无外链、无图片、无网络字体），尺寸同为
   1200×2200，浏览器 / 微信里都能直接打开和转发。

降级原则（本模块最重要的一条）：

- 检测不到 Pillow → 自动输出 SVG，并把 ``degraded=True`` 和**中文原因**返回给调用方；
- 装了 Pillow 但找不到中文字体 → 不画「豆腐块」假图，同样降级为 SVG 并说明原因；
- 调用方显式要求 PNG 而环境不满足 → 仍然降级，但 ``reason`` 会写清是「你要求的 PNG 没做出来」；
- **任何情况下都不会静默失败，也不会假装成功**：返回值里永远带着 ``format`` 与 ``reason``。

长图内容（自上而下）：标题与 slogan → 时间轴（横轴时间 / 气泡面积 ∝ 金额 / 颜色 ∝ 时段）
→ 人生转折点显式标注 → 分期色带与图例 → 六条里程碑 → 人生旁白 → 数据来源、诚实性说明与免责声明。

两种渲染器共用同一份「场景」（:func:`build_scene`）与同一份「版面」（:func:`compute_layout`），
因此 PNG 与 SVG 的内容**逐字一致**，不会出现「PNG 有某句话、SVG 没有」的情况。
"""

from __future__ import annotations

import io
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .chronicle import (
    PHASE_BURNOUT,
    PHASE_EXPLORE,
    PHASE_FIRST,
    PHASE_REVIVAL,
    PHASE_STABLE,
    PHASE_UNKNOWN,
    TIME_SLOTS,
)
from .mcd_adapter import format_yuan

# --------------------------------------------------------------------------- #
# Pillow 可选依赖探测（不做任何隐式安装，也不吞掉错误）
# --------------------------------------------------------------------------- #

PIL_IMPORT_ERROR: Optional[str] = None
try:  # pragma: no cover - 依赖运行环境
    from PIL import Image, ImageDraw, ImageFont  # type: ignore

    _PIL_IMPORTED = True
except Exception as exc:  # pragma: no cover - 依赖运行环境
    _PIL_IMPORTED = False
    PIL_IMPORT_ERROR = "%s: %s" % (type(exc).__name__, exc)


def pillow_available() -> bool:
    """返回 Pillow 是否可用（测试可改写 ``card._PIL_IMPORTED`` 来模拟缺失）。"""
    return bool(_PIL_IMPORTED)


# --------------------------------------------------------------------------- #
# 画布与视觉常量
# --------------------------------------------------------------------------- #

CANVAS_WIDTH = 1200
CANVAS_HEIGHT = 2200

MARGIN_X = 64
CONTENT_WIDTH = CANVAS_WIDTH - MARGIN_X * 2

COLOR_BG = "#0E1116"
COLOR_PANEL = "#161B22"
COLOR_PANEL_SOFT = "#1D242D"
COLOR_TEXT = "#F2F5F9"
COLOR_MUTED = "#98A2B3"
COLOR_ACCENT = "#F5C542"
COLOR_WARN = "#FF8A5B"
COLOR_LINE = "#2A323C"

#: 时段 → 气泡颜色（气泡颜色只表达时段，不表达金额；金额靠面积）。
SLOT_COLORS: Dict[str, str] = {
    "早晨": "#5BC8AF",
    "午市": "#F5C542",
    "下午茶": "#C08BF0",
    "晚市": "#5A9BF6",
    "深夜": "#FF6B6B",
    "时段未核实": "#8A94A6",
}

#: 阶段 → 色带颜色。
PHASE_COLORS: Dict[str, str] = {
    PHASE_FIRST: "#F5C542",
    PHASE_EXPLORE: "#6FD3C7",
    PHASE_STABLE: "#7FA8FF",
    PHASE_BURNOUT: "#8A94A6",
    PHASE_REVIVAL: "#FF8A5B",
    PHASE_UNKNOWN: "#4A525E",
}

#: 时间轴几何。``_timeline_top`` 会根据头部实际高度算出时间轴顶部，避免版面重叠。
LANE_HEIGHT = 58
LANE_LABEL_WIDTH = 84
PLOT_X0 = MARGIN_X + LANE_LABEL_WIDTH
PLOT_X1 = CANVAS_WIDTH - MARGIN_X - 8
PLOT_WIDTH = PLOT_X1 - PLOT_X0
BUBBLE_MIN_RADIUS = 8.0
BUBBLE_MAX_RADIUS = 27.0
REFUNDED_RADIUS = 9.0
CANVAS_TOP = 46

#: 同一时段内相距过近的两个点会做确定性错位，避免完全重叠（单位：像素）。
NUDGE_THRESHOLD = 20.0
NUDGE_OFFSET = 7.0

#: 字号与行高。
FONT_TITLE = 56
FONT_SLOGAN = 26
FONT_SUBTITLE = 21
FONT_SECTION = 20
FONT_BODY = 19
FONT_META = 17
FONT_LANE = 15
FONT_LEGEND = 16
FONT_MS_TITLE = 17
FONT_MS_VALUE = 26
FONT_MS_DETAIL = 18
FONT_FOOTER = 16

BODY_LINE_HEIGHT = 30
DETAIL_LINE_HEIGHT = 25
TURN_LINE_HEIGHT = 24
FOOTER_LINE_HEIGHT = 22
MILESTONE_ROW_HEIGHT = 150

#: 里程碑卡片里最多显示几行补充说明（超出以「…」结尾，避免撑破卡片）。
MILESTONE_DETAIL_MAX_LINES = 2

#: SVG 字体族：只用系统字体名，不引用任何网络字体，保证文件自包含。
SVG_FONT_FAMILY = (
    "'PingFang SC','Microsoft YaHei','Hiragino Sans GB','Source Han Sans SC',"
    "'Noto Sans CJK SC','WenQuanYi Zen Hei','Heiti SC',sans-serif"
)

#: 常见中文字体候选路径（覆盖 Windows / macOS / Linux）。
CJK_FONT_CANDIDATES: Tuple[str, ...] = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "C:/Windows/Fonts/Deng.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
)

#: 环境变量覆盖字体路径（给字体装在别处的用户留的活口）。
FONT_ENV_VAR = "MCD_CARD_FONT"

#: 四种降级原因的中文文案（面向用户，必须原样展示）。
DEGRADE_MESSAGES: Dict[str, str] = {
    "no_pillow": (
        "未检测到 Pillow（本项目的可选依赖，未安装不影响功能）。已自动降级输出自包含 SVG 长图，"
        "尺寸同为 %d×%d，可直接在浏览器或微信中打开与分享。"
        "想要 PNG 输出，请执行 `pip install pillow` 后重跑。" % (CANVAS_WIDTH, CANVAS_HEIGHT)
    ),
    "no_cjk_font": (
        "已安装 Pillow，但在本机没有找到能渲染中文的字体，画出来会是乱码方块，因此**没有**生成 PNG"
        "（不做豆腐块假图）。已自动降级输出自包含 SVG 长图；"
        "可设置环境变量 %s 指向一个中文字体文件后重跑。" % FONT_ENV_VAR
    ),
    "forced_svg": "调用方显式要求 SVG 输出，因此没有生成 PNG。",
    "png_requested_but_unavailable": "调用方要求 PNG，但当前环境不满足 PNG 输出条件，已降级为 SVG（原因见下）。",
}


class CardError(RuntimeError):
    """渲染失败且无法降级时抛出。"""


@dataclass
class RenderResult:
    """渲染结果。调用方必须把 ``format`` / ``degraded`` / ``reason`` 如实告诉用户。"""

    path: str
    """输出文件的绝对路径。"""

    format: str
    """``"png"`` 或 ``"svg"``。"""

    degraded: bool
    """是否为降级输出（``True`` 表示不是最初期望的 PNG）。"""

    reason: Optional[str]
    """降级原因（中文，面向用户）；未降级时为 ``None``。"""

    width: int = CANVAS_WIDTH
    height: int = CANVAS_HEIGHT
    bytes_written: int = 0

    def notice(self) -> str:
        """返回给终端打印的中文说明。"""
        if not self.degraded:
            return "已生成 PNG：%s（%d×%d，%d 字节）" % (self.path, self.width, self.height, self.bytes_written)
        return "已降级输出 SVG：%s（%d×%d，%d 字节）\n  降级原因：%s" % (
            self.path,
            self.width,
            self.height,
            self.bytes_written,
            self.reason or "未说明",
        )


# --------------------------------------------------------------------------- #
# 字体发现
# --------------------------------------------------------------------------- #


def find_cjk_font(candidates: Optional[Sequence[str]] = None) -> Optional[str]:
    """在本机查找一个可用的中文字体文件。

    参数:
        candidates: 自定义候选路径序列；``None`` 时使用 :data:`CJK_FONT_CANDIDATES`。
            环境变量 :data:`FONT_ENV_VAR` 指定的路径拥有最高优先级。

    返回:
        字体文件路径；找不到返回 ``None``（调用方据此决定是否降级）。
    """
    override = os.environ.get(FONT_ENV_VAR, "").strip()
    search: List[str] = []
    if override:
        search.append(override)
    search.extend(candidates if candidates is not None else CJK_FONT_CANDIDATES)

    for path in search:
        if path and os.path.isfile(path):
            return path
    return None


# --------------------------------------------------------------------------- #
# 文本工具（PNG 与 SVG 共用，保证两种输出排版一致）
# --------------------------------------------------------------------------- #


def char_units(text: str) -> float:
    """估算文本显示宽度，以「半角字符宽度」为单位。

    参数:
        text: 任意文本。

    返回:
        宽度单位数：全角字符（含中文）计 2，其余计 1。
    """
    units = 0.0
    for char in text:
        units += 2.0 if ord(char) > 0x2E7F else 1.0
    return units


def wrap_text(text: str, area_width: int, font_size: int, max_lines: int = 0) -> List[str]:
    """按显示宽度折行（中文友好，纯函数、可复现）。

    参数:
        text: 待折行文本；其中的换行符会被当成强制换行。
        area_width: 可用像素宽度。
        font_size: 字号，用于把像素宽度换算成「半角宽度单位」。
        max_lines: 最多输出几行；``0`` 表示不限制。超出时最后一行以 ``…`` 结尾。

    返回:
        折行后的字符串列表（至少一行）。
    """
    if area_width <= 0 or font_size <= 0:
        return [str(text)]

    capacity = max(area_width * 2.0 / font_size, 1.0)
    lines: List[str] = []

    for raw_line in str(text).split("\n"):
        if raw_line == "":
            lines.append("")
            continue
        current = ""
        current_units = 0.0
        for char in raw_line:
            unit = 2.0 if ord(char) > 0x2E7F else 1.0
            if current and current_units + unit > capacity:
                lines.append(current)
                current = char
                current_units = unit
            else:
                current += char
                current_units += unit
        lines.append(current)

    if max_lines and len(lines) > max_lines:
        kept = lines[:max_lines]
        last = kept[-1]
        while last and char_units(last + "…") > capacity:
            last = last[:-1]
        kept[-1] = last + "…"
        return kept

    return lines or [""]


def _format_day(offset: Optional[float]) -> str:
    """把「距首单天数」格式化成标签。"""
    if offset is None:
        return "时间未核实"
    return "第 %d 天" % int(round(offset))


def _header_height(meta_count: int, headline_count: int) -> int:
    """计算头部区块的实际像素高度（两种渲染器共用同一套算式）。

    参数:
        meta_count: 元信息行数。
        headline_count: 判词行数。

    返回:
        头部高度（像素）。
    """
    return (
        FONT_TITLE
        + 16
        + FONT_SLOGAN
        + 12
        + FONT_SUBTITLE
        + 14
        + meta_count * (FONT_META + 9)
        + 2
        + headline_count * 26
        + 4
        + 30
    )


# --------------------------------------------------------------------------- #
# 场景构建（内容与坐标的唯一来源，PNG / SVG 共用）
# --------------------------------------------------------------------------- #


def _bubble_radius(amount_cents: int, amounts: Sequence[int]) -> float:
    """把金额换算成气泡半径，**面积严格与金额成正比**。

    参数:
        amount_cents: 该单净额（分）。
        amounts: 窗口内所有净额，用于确定最大值。

    返回:
        半径（像素）。

    说明:
        公式为 ``r = R_max · √(金额 / 最大金额)``：因为面积 = ``πr²``，
        所以面积与金额严格成正比。如果直接用半径正比金额，视觉面积会变成平方关系，
        10 元的单看起来会是 100 元的单的百分之一——那是图表在说谎。

        ``BUBBLE_MIN_RADIUS`` 只是「金额小到气泡几乎看不见时」的显示下限
        （否则 1 元的订单会退化成一个肉眼不可见的点），金额为 0（含整单退款）
        固定用 ``REFUNDED_RADIUS`` 画空心点，不参与面积比较。
    """
    values = [v for v in amounts if v > 0]
    if not values or amount_cents <= 0:
        return BUBBLE_MIN_RADIUS
    highest = max(values)
    if highest <= 0:
        return BUBBLE_MIN_RADIUS
    radius = BUBBLE_MAX_RADIUS * math.sqrt(amount_cents / float(highest))
    return max(radius, BUBBLE_MIN_RADIUS)


def _timeline_top(header_height: int) -> float:
    """根据头部实际高度推算时间轴顶部 y 坐标（避免版面重叠）。

    参数:
        header_height: 头部区块高度（由 :func:`_header_height` 算出）。

    返回:
        时间轴面板顶部 y 坐标。
    """
    return CANVAS_TOP + header_height + 16 + 30 + 8


def _lane_center_y(slot: str, timeline_top: float) -> float:
    """时段泳道的中心 y 坐标。"""
    index = TIME_SLOTS.index(slot) if slot in TIME_SLOTS else len(TIME_SLOTS) - 1
    return timeline_top + index * LANE_HEIGHT + LANE_HEIGHT / 2.0


def _x_for_offset(offset: Optional[float], max_offset: Optional[float]) -> float:
    """把「距首单天数」映射到 x 坐标（纯函数）。

    参数:
        offset: 距首单天数；``None``（时间缺失）会被放到绘图区中线上。
        max_offset: 最大天数；``None`` 或 0 时所有点都落在中线。
    """
    center = PLOT_X0 + PLOT_WIDTH / 2.0
    if offset is None or max_offset is None or max_offset <= 0:
        return center
    ratio = min(max(offset / max_offset, 0.0), 1.0)
    return PLOT_X0 + ratio * PLOT_WIDTH


def build_scene(report: Dict[str, Any]) -> Dict[str, Any]:
    """把报告字典编译成「画什么、画在哪」的中间场景。

    参数:
        report: ``demo.build_report`` 产生的报告字典。

    返回:
        场景字典（纯数据，无 Pillow 依赖），含 ``header`` / ``timeline`` / ``badges`` /
        ``turnpoint_lines`` / ``phase_bands`` / ``legends`` / ``milestones`` / ``narrative`` /
        ``footer_lines`` / ``generated_at``。
    """
    window = report.get("window", {})
    timeline = list(report.get("timeline", []))
    narrative = report.get("narrative", {})
    phases = report.get("phases", {})
    totals = report.get("totals", {})
    source = report.get("source", {})

    # ---------------- 头部 ----------------
    span_text = ("%.0f 天" % window["span_days"]) if window.get("span_days") is not None else "天数未核实"
    meta_lines = [
        "%s ~ %s · 跨 %s · %d 单（%d 单计入消费）"
        % (
            window.get("first_at") or "时间未核实",
            window.get("last_at") or "时间未核实",
            span_text,
            window.get("order_count", 0),
            window.get("counted_orders", 0),
        ),
        "累计消费 %s · 平均客单价 %s"
        % (
            format_yuan(totals.get("net_cents")),
            format_yuan(totals.get("avg_ticket_cents")) if totals.get("avg_ticket_cents") is not None else "未核实",
        ),
        "数据来源：%s" % source.get("label", "未标注"),
    ]

    header = {
        "title": "麦门人生报告",
        "slogan": "别人问你喜欢吃什么；我们问你这些年到底吃了什么。",
        "subtitle": "你的点餐史，就是你的人生编年史。",
        "meta_lines": meta_lines,
        "headline_lines": wrap_text(str(narrative.get("headline", "")), CONTENT_WIDTH, FONT_BODY, max_lines=2),
        "tags": list(narrative.get("tags", [])),
        "flow": phases.get("flow", PHASE_UNKNOWN),
    }

    # ---------------- 时间轴 ----------------
    header_height = _header_height(len(meta_lines), len(header["headline_lines"]))
    timeline_top = _timeline_top(header_height)
    amounts = [int(point.get("net_cents", 0)) for point in timeline]
    offsets = [point.get("day_offset") for point in timeline if point.get("day_offset") is not None]
    max_offset = max(offsets) if offsets else None

    points: List[Dict[str, Any]] = []
    last_x_by_lane: Dict[str, float] = {}
    nudge_counter: Dict[str, int] = {}
    for point in timeline:
        offset = point.get("day_offset")
        slot = str(point.get("slot", "时段未核实"))
        amount = int(point.get("net_cents", 0))
        refunded = bool(point.get("refunded"))
        radius = REFUNDED_RADIUS if refunded else _bubble_radius(amount, amounts)
        x = _x_for_offset(offset, max_offset)
        y = _lane_center_y(slot, timeline_top)

        # 确定性错位：同一泳道里与前一个点太近时，交替上下偏移，避免完全重叠。
        previous_x = last_x_by_lane.get(slot)
        if previous_x is not None and abs(x - previous_x) < NUDGE_THRESHOLD:
            step = nudge_counter.get(slot, 0)
            y += NUDGE_OFFSET if step % 2 == 0 else -NUDGE_OFFSET
            nudge_counter[slot] = step + 1
        last_x_by_lane[slot] = x

        points.append(
            {
                "index": point.get("index"),
                "x": x,
                "y": y,
                "r": radius,
                "color": SLOT_COLORS.get(slot, SLOT_COLORS["时段未核实"]),
                "phase": point.get("phase"),
                "phase_color": PHASE_COLORS.get(str(point.get("phase")), PHASE_COLORS[PHASE_UNKNOWN]),
                "slot": slot,
                "refunded": refunded,
                "is_origin": bool(point.get("is_origin")),
                "is_turn_point": bool(point.get("is_turn_point")),
                "amount_text": format_yuan(point.get("net_cents")),
                "at_text": point.get("at") or "时间未核实",
                "items_text": str(point.get("top_items", "")),
                "day_text": _format_day(offset),
            }
        )

    # x 轴刻度：按「月」打点，最多 8 个，避免拥挤。
    month_seen: Dict[str, float] = {}
    for point in timeline:
        at = point.get("at")
        offset = point.get("day_offset")
        if not at or offset is None:
            continue
        key = str(at)[:7]
        if key not in month_seen:
            month_seen[key] = _x_for_offset(offset, max_offset)
    tick_items = sorted(month_seen.items(), key=lambda pair: pair[1])
    if len(tick_items) > 8:
        step = int(math.ceil(len(tick_items) / 8.0))
        tick_items = tick_items[::step]

    timeline_block = {
        "top": timeline_top,
        "height": LANE_HEIGHT * len(TIME_SLOTS),
        "lane_labels": [
            {"y": _lane_center_y(slot, timeline_top), "text": slot, "color": SLOT_COLORS[slot]} for slot in TIME_SLOTS
        ],
        "points": points,
        "ticks": [{"x": x, "text": key} for key, x in tick_items],
    }

    # ---------------- 转折点 ----------------
    turn_raw: List[str] = []
    turn_points = [point for point in timeline if point.get("is_turn_point")]
    for order_index, point in enumerate(turn_points, start=1):
        reasons = "；".join(point.get("turn_reasons", [])) or "关系状态发生变化"
        turn_raw.append(
            "%d. %s｜%s｜%s" % (order_index, point.get("at") or "时间未核实", format_yuan(point.get("net_cents")), reasons)
        )
    wrapped_turns: List[str] = []
    for line in turn_raw[:5]:
        wrapped_turns.extend(wrap_text(line, CONTENT_WIDTH, FONT_MS_DETAIL, max_lines=2))
    if len(turn_raw) > 5:
        wrapped_turns.append("…另有 %d 个转折点未在图上展开。" % (len(turn_raw) - 5))

    badges = []
    x_by_index = {point["index"]: point["x"] for point in points if point.get("index") is not None}
    for order_index, point in enumerate(turn_points, start=1):
        index = point.get("index")
        if order_index > 5 or index is None or index not in x_by_index:
            continue
        badges.append({"x": x_by_index[index], "y": timeline_top - 16, "text": str(order_index)})

    # ---------------- 分期色带 ----------------
    bands: List[Dict[str, Any]] = []
    for segment in phases.get("segments", []):
        start_x = _x_for_offset(segment.get("start_offset"), max_offset)
        end_x = _x_for_offset(segment.get("end_offset"), max_offset)
        if end_x < start_x:
            start_x, end_x = end_x, start_x
        width = max(end_x - start_x, 3.0)
        label = str(segment.get("phase", PHASE_UNKNOWN))
        bands.append(
            {
                "x": start_x,
                "width": width,
                "color": PHASE_COLORS.get(label, PHASE_COLORS[PHASE_UNKNOWN]),
                "label": label if width >= 72 else "",
            }
        )

    # ---------------- 图例 ----------------
    legends = [
        {
            "title": "气泡颜色 = 时段",
            "items": [{"color": SLOT_COLORS[slot], "text": slot} for slot in TIME_SLOTS],
            "note": "",
        },
        {
            "title": "色带 = 人生阶段",
            "items": [
                {"color": PHASE_COLORS[PHASE_FIRST], "text": PHASE_FIRST},
                {"color": PHASE_COLORS[PHASE_EXPLORE], "text": PHASE_EXPLORE},
                {"color": PHASE_COLORS[PHASE_STABLE], "text": PHASE_STABLE},
                {"color": PHASE_COLORS[PHASE_REVIVAL], "text": PHASE_REVIVAL},
                {"color": PHASE_COLORS[PHASE_BURNOUT], "text": PHASE_BURNOUT},
            ],
            "note": "面积 ∝ 金额 ｜ 空心点 = 整单退款（不计入消费额）",
        },
    ]

    # ---------------- 里程碑（2 列 × 3 行） ----------------
    cell_width = (CONTENT_WIDTH - 24) // 2
    cells: List[Dict[str, Any]] = []
    for milestone in report.get("milestones", []):
        cells.append(
            {
                "title": milestone.get("title", ""),
                "display": milestone.get("display", "未核实"),
                "available": bool(milestone.get("available")),
                "detail_lines": wrap_text(
                    str(milestone.get("detail", "")),
                    cell_width - 36,
                    FONT_MS_DETAIL,
                    max_lines=MILESTONE_DETAIL_MAX_LINES,
                ),
                "col": len(cells) % 2,
                "row": len(cells) // 2,
            }
        )

    # ---------------- 旁白 ----------------
    narration_lines: List[str] = []
    for paragraph in narrative.get("paragraphs", []):
        narration_lines.extend(wrap_text("· " + str(paragraph), CONTENT_WIDTH, FONT_BODY))

    # ---------------- 诚实性说明（只放最要紧的，其余指向 README） ----------------
    honesty_source = [str(note).replace("**", "") for note in narrative.get("honesty", [])]
    honesty_lines: List[str] = []
    if honesty_source:
        honesty_lines.extend(wrap_text("· " + honesty_source[0], CONTENT_WIDTH, FONT_FOOTER, max_lines=2))
        if len(honesty_source) > 1:
            honesty_lines.append(
                "……另有 %d 条诚实性说明（退款剔除口径、确定性规则、窗口截断），完整内容见 README。"
                % (len(honesty_source) - 1)
            )
    else:
        honesty_lines.append("· 本图未附诚实性说明，请以 README 为准。")

    footer_lines = [
        "数据来源：麦当劳 MCP 只读工具 order-list / query-my-account / query-my-coupons / now-time-info",
        "本图为参赛作品输出，非麦当劳官方产品；输出仅供参考，不构成任何专业建议。",
        source.get("notice", "示例数据说明：未标注。"),
    ]

    return {
        "width": CANVAS_WIDTH,
        "height": CANVAS_HEIGHT,
        "generated_at": report.get("generated_at", ""),
        "header": header,
        "header_height": header_height,
        "timeline": timeline_block,
        "badges": badges,
        "turnpoint_lines": wrapped_turns,
        "phase_bands": bands,
        "legends": legends,
        "milestones": cells,
        "milestone_cell_width": cell_width,
        "narrative": {
            "lines": narration_lines,
            "verdict_lines": wrap_text(str(narrative.get("verdict", "")), CONTENT_WIDTH, FONT_BODY, max_lines=2),
            "honesty_lines": honesty_lines,
        },
        "footer_lines": footer_lines,
    }


# --------------------------------------------------------------------------- #
# 版面计算（两种渲染器共用）
# --------------------------------------------------------------------------- #


def compute_layout(scene: Dict[str, Any]) -> Dict[str, Any]:
    """计算各版块的 y 坐标（PNG 与 SVG 共用的唯一版面来源）。

    参数:
        scene: :func:`build_scene` 的输出。

    返回:
        版块名 → y 坐标 / 高度 的字典。

    说明:
        采用「自上而下逐个量高 + 自下而上固定页脚」的流式布局：
        头部、时间轴、转折点、色带、图例、里程碑按各自实际行数依次下推，
        「人生旁白」自动吃掉剩余空间（放不下时由 :func:`_fit_lines` 显式标注），
        因此改动文案长度不会导致版块重叠。
    """
    header_height = int(scene.get("header_height") or _header_height(len(scene["header"]["meta_lines"]), len(scene["header"]["headline_lines"])))
    timeline_top = _timeline_top(header_height)

    y = CANVAS_TOP + header_height
    y_timeline_title = y + 16
    y = y_timeline_title + 30 + 8

    y_tick = y + LANE_HEIGHT * len(TIME_SLOTS) + 12
    y_turn_title = y_tick + 22 + 14
    y_turn_body = y_turn_title + 30
    y = y_turn_body + max(len(scene["turnpoint_lines"]), 1) * TURN_LINE_HEIGHT + 18

    y_band = y
    y = y_band + 22 + 24

    y_legend = y
    for legend in scene["legends"]:
        y += 26
        if legend.get("note"):
            y += 22

    y_ms_title = y + 20
    y_grid = y_ms_title + 34
    rows = max(1, (len(scene["milestones"]) + 1) // 2)
    y_narrative_title = y_grid + rows * MILESTONE_ROW_HEIGHT + 20
    y_narrative = y_narrative_title + 34

    honesty_lines = scene["narrative"]["honesty_lines"]
    honesty_height = 26 + max(len(honesty_lines), 1) * FOOTER_LINE_HEIGHT
    footer_height = (1 if scene.get("generated_at") else 0) * FOOTER_LINE_HEIGHT + len(scene["footer_lines"]) * FOOTER_LINE_HEIGHT
    y_footer = CANVAS_HEIGHT - 34 - footer_height
    y_honesty = y_footer - 16 - honesty_height
    y_verdict = y_honesty - 16 - 62

    return {
        "header": CANVAS_TOP,
        "timeline_title": y_timeline_title,
        "timeline_top": timeline_top,
        "tick": y_tick,
        "turn_title": y_turn_title,
        "turn_body": y_turn_body,
        "band": y_band,
        "legend": y_legend,
        "milestone_title": y_ms_title,
        "grid": y_grid,
        "row_height": MILESTONE_ROW_HEIGHT,
        "narrative_title": y_narrative_title,
        "narrative": y_narrative,
        "verdict_top": y_verdict,
        "honesty": y_honesty,
        "footer": y_footer,
    }


def _fit_lines(lines: Sequence[str], line_height: int, available_height: int, overflow_note: str) -> List[str]:
    """在有限高度内裁剪文本，溢出时追加一条显式说明（绝不静默截断）。

    参数:
        lines: 已折行的文本行。
        line_height: 单行高度。
        available_height: 可用高度。
        overflow_note: 溢出时追加的说明文本。

    返回:
        裁剪后的行列表。
    """
    capacity = int(available_height // line_height)
    if capacity <= 0:
        return []
    kept = [line for line in lines]
    while kept and kept[-1] == "":
        kept.pop()
    if len(kept) <= capacity:
        return kept
    trimmed = kept[: max(capacity - 1, 1)]
    trimmed.append(overflow_note)
    return trimmed


# --------------------------------------------------------------------------- #
# PNG 渲染（Pillow，可选依赖）
# --------------------------------------------------------------------------- #


def _rgb(hex_color: str) -> Tuple[int, int, int]:
    """把 ``#RRGGBB`` 转成 RGB 三元组。"""
    value = hex_color.lstrip("#")
    if len(value) == 3:
        value = "".join(char * 2 for char in value)
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


_FONT_CACHE: Dict[Tuple[str, int], Any] = {}


def _load_font(font_path: str, size: int) -> Any:
    """加载并缓存字体对象。"""
    key = (font_path, size)
    if key not in _FONT_CACHE:
        _FONT_CACHE[key] = ImageFont.truetype(font_path, size)
    return _FONT_CACHE[key]


def _text_width(draw: Any, text: str, font: Any) -> float:
    """测量文本像素宽度（兼容不同 Pillow 版本）。"""
    try:
        return float(draw.textlength(text, font=font))
    except Exception:  # pragma: no cover - 老版本 Pillow
        try:
            return float(font.getsize(text)[0])
        except Exception:
            return float(char_units(text) * 8)


def _rounded_rect(draw: Any, box: Sequence[Tuple[float, float]], radius: int, fill: str) -> None:
    """画圆角矩形；老版本 Pillow 没有 ``rounded_rectangle`` 时退回直角矩形。"""
    if hasattr(draw, "rounded_rectangle"):
        draw.rounded_rectangle(list(box), radius=radius, fill=_rgb(fill))
    else:  # pragma: no cover - 老版本 Pillow
        draw.rectangle(list(box), fill=_rgb(fill))


def _draw_dashed_v(draw: Any, x: float, y0: float, y1: float, color: str, dash: int = 6, gap: int = 6) -> None:
    """画一条竖直虚线。"""
    cursor = y0
    while cursor < y1:
        draw.line([(x, cursor), (x, min(cursor + dash, y1))], fill=_rgb(color), width=2)
        cursor += dash + gap


def render_png_bytes(scene: Dict[str, Any]) -> bytes:
    """用 Pillow 把场景渲染成 PNG 字节流。

    参数:
        scene: :func:`build_scene` 的输出。

    返回:
        PNG 字节流。

    异常:
        CardError: Pillow 不可用或找不到中文字体时抛出（调用方应改为输出 SVG）。
    """
    if not pillow_available():
        raise CardError("Pillow 不可用：%s" % (PIL_IMPORT_ERROR or "未安装"))

    font_path = find_cjk_font()
    if not font_path:
        raise CardError("未找到可渲染中文的字体文件，拒绝输出乱码图。")

    layout = compute_layout(scene)
    image = Image.new("RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), _rgb(COLOR_BG))
    draw = ImageDraw.Draw(image)

    def text(x: float, y: float, content: str, size: int, color: str = COLOR_TEXT, center: bool = False) -> None:
        if not content:
            return
        chosen = _load_font(font_path, size)
        draw_x = x - _text_width(draw, content, chosen) / 2.0 if center else x
        draw.text((draw_x, y), content, font=chosen, fill=_rgb(color))

    def line(x0: float, y0: float, x1: float, y1: float, color: str, width: int = 1) -> None:
        draw.line([(x0, y0), (x1, y1)], fill=_rgb(color), width=width)

    # ---------------- 头部 ----------------
    y = layout["header"]
    text(MARGIN_X, y, scene["header"]["title"], FONT_TITLE, COLOR_TEXT)
    y += FONT_TITLE + 16
    text(MARGIN_X, y, scene["header"]["slogan"], FONT_SLOGAN, COLOR_ACCENT)
    y += FONT_SLOGAN + 12
    text(MARGIN_X, y, scene["header"]["subtitle"], FONT_SUBTITLE, COLOR_MUTED)
    y += FONT_SUBTITLE + 14
    for meta in scene["header"]["meta_lines"]:
        text(MARGIN_X, y, meta, FONT_META, COLOR_MUTED)
        y += FONT_META + 9
    y += 2
    for head_line in scene["header"]["headline_lines"]:
        text(MARGIN_X, y, head_line, FONT_BODY, COLOR_TEXT)
        y += 26
    y += 4

    pill_x = MARGIN_X
    pill_y = y
    for tag in scene["header"]["tags"][:3]:
        width = _text_width(draw, tag, _load_font(font_path, FONT_META)) + 26
        _rounded_rect(draw, [(pill_x, pill_y), (pill_x + width, pill_y + 30)], 15, COLOR_PANEL_SOFT)
        text(pill_x + 13, pill_y + 6, tag, FONT_META, COLOR_ACCENT)
        pill_x += width + 10
    text(pill_x + 6, pill_y + 6, "分期流水：" + scene["header"]["flow"], FONT_META, COLOR_MUTED)

    # ---------------- 时间轴 ----------------
    text(
        MARGIN_X,
        layout["timeline_title"],
        "时间轴编年史（横轴 = 时间，气泡面积 = 金额，颜色 = 时段）",
        FONT_SECTION,
        COLOR_TEXT,
    )
    block = scene["timeline"]
    _rounded_rect(
        draw,
        [(MARGIN_X, block["top"] - 6), (CANVAS_WIDTH - MARGIN_X, block["top"] + block["height"] + 6)],
        12,
        COLOR_PANEL,
    )
    for lane in block["lane_labels"]:
        text(MARGIN_X + 12, lane["y"] - 8, lane["text"], FONT_LANE, lane["color"])
        line(PLOT_X0, lane["y"], PLOT_X1, lane["y"], COLOR_LINE, 1)

    band_y = block["top"] + block["height"] - 16
    for band in scene["phase_bands"]:
        draw.rectangle([(band["x"], band_y), (band["x"] + band["width"], band_y + 8)], fill=_rgb(band["color"]))

    for point in block["points"]:
        x, y_pos, radius = point["x"], point["y"], point["r"]
        if point["refunded"]:
            draw.ellipse(
                [(x - radius, y_pos - radius), (x + radius, y_pos + radius)],
                outline=_rgb(COLOR_MUTED),
                width=2,
            )
            line(x - radius, y_pos + radius, x + radius, y_pos - radius, COLOR_MUTED, 2)
        else:
            draw.ellipse(
                [(x - radius, y_pos - radius), (x + radius, y_pos + radius)],
                fill=_rgb(point["color"]),
                outline=_rgb(COLOR_BG),
                width=1,
            )
        if point["is_turn_point"]:
            draw.ellipse(
                [(x - radius - 6, y_pos - radius - 6), (x + radius + 6, y_pos + radius + 6)],
                outline=_rgb(COLOR_WARN),
                width=2,
            )

    for tick in block["ticks"]:
        line(tick["x"], block["top"] + 4, tick["x"], block["top"] + 12, COLOR_MUTED, 1)
        text(tick["x"], layout["tick"], tick["text"], FONT_LEGEND, COLOR_MUTED, center=True)

    for badge in scene["badges"]:
        _draw_dashed_v(draw, badge["x"], block["top"] - 10, block["top"] + block["height"] - 10, COLOR_WARN)
        draw.ellipse([(badge["x"] - 13, badge["y"] - 13), (badge["x"] + 13, badge["y"] + 13)], fill=_rgb(COLOR_WARN))
        text(badge["x"], badge["y"] - 10, badge["text"], FONT_LEGEND, COLOR_BG, center=True)

    # ---------------- 转折点 ----------------
    text(MARGIN_X, layout["turn_title"], "人生转折点（时间轴上的橙色方框点）", FONT_SECTION, COLOR_TEXT)
    y = layout["turn_body"]
    if not scene["turnpoint_lines"]:
        text(MARGIN_X, y, "窗口内没有触发转折点判据的订单。", FONT_MS_DETAIL, COLOR_MUTED)
    for turn_line in scene["turnpoint_lines"]:
        text(MARGIN_X, y, turn_line, FONT_MS_DETAIL, COLOR_TEXT)
        y += TURN_LINE_HEIGHT

    # ---------------- 分期色带标签 ----------------
    band_y = layout["band"]
    for band in scene["phase_bands"]:
        draw.rectangle([(band["x"], band_y), (band["x"] + band["width"], band_y + 22)], fill=_rgb(band["color"]))
        if band["label"]:
            text(band["x"] + band["width"] / 2.0, band_y + 1, band["label"], FONT_LEGEND, COLOR_BG, center=True)

    # ---------------- 图例 ----------------
    y = layout["legend"]
    for legend in scene["legends"]:
        x = MARGIN_X
        text(x, y, legend["title"], FONT_LEGEND, COLOR_MUTED)
        x += 156
        for item in legend["items"]:
            draw.ellipse([(x, y + 2), (x + 14, y + 16)], fill=_rgb(item["color"]))
            text(x + 20, y, item["text"], FONT_LEGEND, COLOR_TEXT)
            x += 20 + _text_width(draw, item["text"], _load_font(font_path, FONT_LEGEND)) + 16
        y += 26
        if legend.get("note"):
            text(MARGIN_X, y, legend["note"], FONT_LEGEND, COLOR_MUTED)
            y += 22

    # ---------------- 里程碑 ----------------
    text(MARGIN_X, layout["milestone_title"], "六条里程碑（全部来自真实订单，口径见 README）", FONT_SECTION, COLOR_TEXT)
    cell_width = scene["milestone_cell_width"]
    for cell in scene["milestones"]:
        x0 = MARGIN_X + cell["col"] * (cell_width + 24)
        y0 = layout["grid"] + cell["row"] * layout["row_height"]
        _rounded_rect(draw, [(x0, y0), (x0 + cell_width, y0 + layout["row_height"] - 14)], 10, COLOR_PANEL)
        text(x0 + 18, y0 + 13, cell["title"], FONT_MS_TITLE, COLOR_MUTED)
        text(x0 + 18, y0 + 36, cell["display"], FONT_MS_VALUE, COLOR_ACCENT if cell["available"] else COLOR_MUTED)
        detail_y = y0 + 74
        for detail in cell["detail_lines"]:
            text(x0 + 18, detail_y, detail, FONT_MS_DETAIL, COLOR_MUTED)
            detail_y += DETAIL_LINE_HEIGHT

    # ---------------- 人生旁白 ----------------
    text(
        MARGIN_X,
        layout["narrative_title"],
        "人生旁白（确定性规则生成，无随机数、无大模型）",
        FONT_SECTION,
        COLOR_TEXT,
    )
    available = layout["verdict_top"] - layout["narrative"]
    narration = _fit_lines(
        scene["narrative"]["lines"],
        BODY_LINE_HEIGHT,
        available,
        "……完整旁白请见 README（本图已放不下，这里如实标注）。",
    )
    y = layout["narrative"]
    for narration_line in narration:
        if narration_line:
            text(MARGIN_X, y, narration_line, FONT_BODY, COLOR_TEXT)
        y += BODY_LINE_HEIGHT

    verdict_top = layout["verdict_top"]
    _rounded_rect(draw, [(MARGIN_X, verdict_top), (CANVAS_WIDTH - MARGIN_X, verdict_top + 62)], 10, COLOR_PANEL_SOFT)
    y = verdict_top + 6
    for verdict_line in scene["narrative"]["verdict_lines"]:
        text(MARGIN_X + 18, y, verdict_line, FONT_BODY, COLOR_ACCENT)
        y += 28

    # ---------------- 诚实性说明 ----------------
    y = layout["honesty"]
    text(MARGIN_X, y, "诚实性说明", FONT_FOOTER, COLOR_WARN)
    y += 26
    for honesty_line in scene["narrative"]["honesty_lines"]:
        text(MARGIN_X, y, honesty_line, FONT_FOOTER, COLOR_MUTED)
        y += FOOTER_LINE_HEIGHT

    # ---------------- 页脚 ----------------
    y = layout["footer"]
    if scene.get("generated_at"):
        text(MARGIN_X, y, "生成时间：" + str(scene["generated_at"]), FONT_FOOTER, COLOR_MUTED)
        y += FOOTER_LINE_HEIGHT
    for index, footer_line in enumerate(scene["footer_lines"]):
        color = COLOR_WARN if index == len(scene["footer_lines"]) - 1 else COLOR_MUTED
        for wrapped in wrap_text(footer_line, CONTENT_WIDTH, FONT_FOOTER, max_lines=1):
            text(MARGIN_X, y, wrapped, FONT_FOOTER, color)
            y += FOOTER_LINE_HEIGHT

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# SVG 渲染（纯标准库，永远可用）
# --------------------------------------------------------------------------- #


def _svg_escape(text: str) -> str:
    """转义 SVG 文本节点中的特殊字符。"""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_svg(scene: Dict[str, Any]) -> str:
    """把场景渲染成**自包含** SVG 字符串（1200×2200，无任何外部引用）。

    参数:
        scene: :func:`build_scene` 的输出。

    返回:
        SVG 文本。

    说明:
        所有样式都是内联属性，字体使用系统字体族回退，因此没有网络请求、也不依赖
        本机存在某个具体字体文件就能正常显示中文。
    """
    layout = compute_layout(scene)
    parts: List[str] = []
    add = parts.append

    add('<?xml version="1.0" encoding="UTF-8"?>')
    add(
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
        'role="img" aria-label="麦门人生报告：点餐史时间轴长图">'
        % (CANVAS_WIDTH, CANVAS_HEIGHT, CANVAS_WIDTH, CANVAS_HEIGHT)
    )
    add("<title>麦门人生报告 · 人生时间轴长图</title>")
    add("<desc>由 mcd-life-report 生成：横轴为时间，气泡面积与净消费金额成正比，颜色表示时段。</desc>")

    def text(
        x: float,
        y: float,
        content: str,
        size: int,
        color: str,
        center: bool = False,
        bold: bool = False,
    ) -> None:
        if not content:
            return
        add(
            '<text x="%.1f" y="%.1f" font-family="%s" font-size="%d" fill="%s"%s%s>%s</text>'
            % (
                x,
                y,
                SVG_FONT_FAMILY,
                size,
                color,
                ' text-anchor="middle"' if center else "",
                ' font-weight="600"' if bold else "",
                _svg_escape(content),
            )
        )

    def rect(x: float, y: float, w: float, h: float, fill: str, radius: int = 0) -> None:
        add(
            '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"%s/>'
            % (x, y, w, h, fill, ' rx="%d"' % radius if radius else "")
        )

    def circle(cx: float, cy: float, r: float, fill: str, stroke: str = "none", width: float = 0.0) -> None:
        add(
            '<circle cx="%.1f" cy="%.1f" r="%.1f" fill="%s" stroke="%s" stroke-width="%.1f"/>'
            % (cx, cy, r, fill, stroke, width)
        )

    def line(x0: float, y0: float, x1: float, y1: float, color: str, width: float = 1.0, dash: str = "") -> None:
        add(
            '<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="%.1f"%s/>'
            % (x0, y0, x1, y1, color, width, ' stroke-dasharray="%s"' % dash if dash else "")
        )

    add('<rect x="0" y="0" width="%d" height="%d" fill="%s"/>' % (CANVAS_WIDTH, CANVAS_HEIGHT, COLOR_BG))

    # ---------------- 头部 ----------------
    y = layout["header"] + FONT_TITLE - 8
    text(MARGIN_X, y, scene["header"]["title"], FONT_TITLE, COLOR_TEXT, bold=True)
    y += 16 + FONT_SLOGAN
    text(MARGIN_X, y, scene["header"]["slogan"], FONT_SLOGAN, COLOR_ACCENT, bold=True)
    y += 12 + FONT_SUBTITLE
    text(MARGIN_X, y, scene["header"]["subtitle"], FONT_SUBTITLE, COLOR_MUTED)
    y += 14 + FONT_META
    for meta in scene["header"]["meta_lines"]:
        text(MARGIN_X, y, meta, FONT_META, COLOR_MUTED)
        y += FONT_META + 9
    y += 2 + FONT_BODY - 6
    for head_line in scene["header"]["headline_lines"]:
        text(MARGIN_X, y, head_line, FONT_BODY, COLOR_TEXT)
        y += 26
    y += 4

    pill_x = MARGIN_X
    pill_y = y - 18
    for tag in scene["header"]["tags"][:3]:
        width = char_units(tag) * FONT_META / 2.0 + 26
        rect(pill_x, pill_y, width, 30, COLOR_PANEL_SOFT, radius=15)
        text(pill_x + 13, pill_y + 21, tag, FONT_META, COLOR_ACCENT)
        pill_x += width + 10
    text(pill_x + 6, pill_y + 21, "分期流水：" + scene["header"]["flow"], FONT_META, COLOR_MUTED)

    # ---------------- 时间轴 ----------------
    text(
        MARGIN_X,
        layout["timeline_title"] + FONT_SECTION - 5,
        "时间轴编年史（横轴 = 时间，气泡面积 = 金额，颜色 = 时段）",
        FONT_SECTION,
        COLOR_TEXT,
        bold=True,
    )
    block = scene["timeline"]
    rect(MARGIN_X, block["top"] - 6, CONTENT_WIDTH, block["height"] + 12, COLOR_PANEL, radius=12)
    for lane in block["lane_labels"]:
        text(MARGIN_X + 12, lane["y"] + 5, lane["text"], FONT_LANE, lane["color"])
        line(PLOT_X0, lane["y"], PLOT_X1, lane["y"], COLOR_LINE, 1)

    band_y = block["top"] + block["height"] - 16
    for band in scene["phase_bands"]:
        rect(band["x"], band_y, band["width"], 8, band["color"])

    for point in block["points"]:
        if point["refunded"]:
            circle(point["x"], point["y"], point["r"], "none", COLOR_MUTED, 2)
            line(point["x"] - point["r"], point["y"] + point["r"], point["x"] + point["r"], point["y"] - point["r"], COLOR_MUTED, 2)
        else:
            circle(point["x"], point["y"], point["r"], point["color"], COLOR_BG, 1)
        if point["is_turn_point"]:
            circle(point["x"], point["y"], point["r"] + 6, "none", COLOR_WARN, 2)

    for tick in block["ticks"]:
        line(tick["x"], block["top"] + 4, tick["x"], block["top"] + 12, COLOR_MUTED, 1)
        text(tick["x"], layout["tick"] + FONT_LEGEND - 3, tick["text"], FONT_LEGEND, COLOR_MUTED, center=True)

    for badge in scene["badges"]:
        line(badge["x"], block["top"] - 10, badge["x"], block["top"] + block["height"] - 10, COLOR_WARN, 2, "6 6")
        circle(badge["x"], badge["y"], 13, COLOR_WARN)
        text(badge["x"], badge["y"] + 6, badge["text"], FONT_LEGEND, COLOR_BG, center=True)

    # ---------------- 转折点 ----------------
    text(MARGIN_X, layout["turn_title"] + FONT_SECTION - 5, "人生转折点（时间轴上的橙色方框点）", FONT_SECTION, COLOR_TEXT, bold=True)
    y = layout["turn_body"] + FONT_MS_DETAIL - 4
    if not scene["turnpoint_lines"]:
        text(MARGIN_X, y, "窗口内没有触发转折点判据的订单。", FONT_MS_DETAIL, COLOR_MUTED)
    for turn_line in scene["turnpoint_lines"]:
        text(MARGIN_X, y, turn_line, FONT_MS_DETAIL, COLOR_TEXT)
        y += TURN_LINE_HEIGHT

    # ---------------- 分期色带 ----------------
    band_y = layout["band"]
    for band in scene["phase_bands"]:
        rect(band["x"], band_y, band["width"], 22, band["color"])
        if band["label"]:
            text(band["x"] + band["width"] / 2.0, band_y + 16, band["label"], FONT_LEGEND, COLOR_BG, center=True)

    # ---------------- 图例 ----------------
    y = layout["legend"] + FONT_LEGEND - 3
    for legend in scene["legends"]:
        x = MARGIN_X
        text(x, y, legend["title"], FONT_LEGEND, COLOR_MUTED)
        x += 156
        for item in legend["items"]:
            circle(x + 7, y - 5, 7, item["color"])
            text(x + 20, y, item["text"], FONT_LEGEND, COLOR_TEXT)
            x += 20 + char_units(item["text"]) * FONT_LEGEND / 2.0 + 16
        y += 26
        if legend.get("note"):
            text(MARGIN_X, y, legend["note"], FONT_LEGEND, COLOR_MUTED)
            y += 22

    # ---------------- 里程碑 ----------------
    text(MARGIN_X, layout["milestone_title"] + FONT_SECTION - 5, "六条里程碑（全部来自真实订单，口径见 README）", FONT_SECTION, COLOR_TEXT, bold=True)
    cell_width = scene["milestone_cell_width"]
    for cell in scene["milestones"]:
        x0 = MARGIN_X + cell["col"] * (cell_width + 24)
        y0 = layout["grid"] + cell["row"] * layout["row_height"]
        rect(x0, y0, cell_width, layout["row_height"] - 14, COLOR_PANEL, radius=10)
        text(x0 + 18, y0 + 27, cell["title"], FONT_MS_TITLE, COLOR_MUTED)
        text(x0 + 18, y0 + 36 + FONT_MS_VALUE, cell["display"], FONT_MS_VALUE, COLOR_ACCENT if cell["available"] else COLOR_MUTED, bold=True)
        detail_y = y0 + 74 + FONT_MS_DETAIL - 4
        for detail in cell["detail_lines"]:
            text(x0 + 18, detail_y, detail, FONT_MS_DETAIL, COLOR_MUTED)
            detail_y += DETAIL_LINE_HEIGHT

    # ---------------- 人生旁白 ----------------
    text(
        MARGIN_X,
        layout["narrative_title"] + FONT_SECTION - 5,
        "人生旁白（确定性规则生成，无随机数、无大模型）",
        FONT_SECTION,
        COLOR_TEXT,
        bold=True,
    )
    available = layout["verdict_top"] - layout["narrative"]
    narration = _fit_lines(
        scene["narrative"]["lines"],
        BODY_LINE_HEIGHT,
        available,
        "……完整旁白请见 README（本图已放不下，这里如实标注）。",
    )
    y = layout["narrative"] + FONT_BODY - 4
    for narration_line in narration:
        if narration_line:
            text(MARGIN_X, y, narration_line, FONT_BODY, COLOR_TEXT)
        y += BODY_LINE_HEIGHT

    verdict_top = layout["verdict_top"]
    rect(MARGIN_X, verdict_top, CONTENT_WIDTH, 62, COLOR_PANEL_SOFT, radius=10)
    y = verdict_top + 6 + FONT_BODY
    for verdict_line in scene["narrative"]["verdict_lines"]:
        text(MARGIN_X + 18, y, verdict_line, FONT_BODY, COLOR_ACCENT)
        y += 28

    # ---------------- 诚实性说明 ----------------
    y = layout["honesty"] + FONT_FOOTER - 3
    text(MARGIN_X, y, "诚实性说明", FONT_FOOTER, COLOR_WARN)
    y += 26
    for honesty_line in scene["narrative"]["honesty_lines"]:
        text(MARGIN_X, y, honesty_line, FONT_FOOTER, COLOR_MUTED)
        y += FOOTER_LINE_HEIGHT

    # ---------------- 页脚 ----------------
    y = layout["footer"] + FONT_FOOTER - 3
    if scene.get("generated_at"):
        text(MARGIN_X, y, "生成时间：" + str(scene["generated_at"]), FONT_FOOTER, COLOR_MUTED)
        y += FOOTER_LINE_HEIGHT
    for index, footer_line in enumerate(scene["footer_lines"]):
        color = COLOR_WARN if index == len(scene["footer_lines"]) - 1 else COLOR_MUTED
        for wrapped in wrap_text(footer_line, CONTENT_WIDTH, FONT_FOOTER, max_lines=1):
            text(MARGIN_X, y, wrapped, FONT_FOOTER, color)
            y += FOOTER_LINE_HEIGHT

    add("</svg>")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #


def render_card(
    report: Dict[str, Any],
    out_dir: str,
    basename: str = "mcd-life-report",
    force_backend: Optional[str] = None,
) -> RenderResult:
    """生成分享长图。

    参数:
        report: ``demo.build_report`` 产生的报告字典。
        out_dir: 输出目录（不存在会自动创建）。
        basename: 文件名前缀（不含扩展名）。
        force_backend: ``None``（自动）/ ``"png"`` / ``"svg"``。供测试与用户显式选择。

    返回:
        :class:`RenderResult`。``format`` / ``degraded`` / ``reason`` 必须被如实展示。

    异常:
        CardError: 输出目录不可写、或 ``force_backend`` 取值非法时抛出。

    说明:
        任何降级情况下本函数都会返回一个**真实存在**的文件路径，不会出现
        「返回成功但文件不存在」。
    """
    if force_backend not in (None, "png", "svg"):
        raise CardError("force_backend 只支持 None/'png'/'svg'，收到：%r" % (force_backend,))

    directory = Path(out_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CardError("无法创建输出目录 %s：%s" % (out_dir, exc))

    scene = build_scene(report)
    reason: Optional[str] = None

    if force_backend != "svg" and pillow_available() and find_cjk_font():
        try:
            payload = render_png_bytes(scene)
        except Exception as exc:  # pragma: no cover - 依赖运行环境
            reason = "PNG 渲染过程出错（%s: %s），已降级为 SVG。" % (type(exc).__name__, exc)
        else:
            png_path = directory / (basename + ".png")
            png_path.write_bytes(payload)
            return RenderResult(
                path=str(png_path.resolve()),
                format="png",
                degraded=False,
                reason=None,
                bytes_written=len(payload),
            )

    if reason is None:
        prefix = DEGRADE_MESSAGES["png_requested_but_unavailable"] + " " if force_backend == "png" else ""
        if force_backend == "svg":
            reason = DEGRADE_MESSAGES["forced_svg"]
        elif not pillow_available():
            reason = prefix + DEGRADE_MESSAGES["no_pillow"]
        else:
            reason = prefix + DEGRADE_MESSAGES["no_cjk_font"]

    svg_text = render_svg(scene)
    svg_path = directory / (basename + ".svg")
    try:
        svg_path.write_text(svg_text, encoding="utf-8")
    except OSError as exc:
        raise CardError("无法写入 SVG 文件 %s：%s" % (svg_path, exc))

    return RenderResult(
        path=str(svg_path.resolve()),
        format="svg",
        degraded=True,
        reason=reason,
        bytes_written=len(svg_text.encode("utf-8")),
    )


def describe_output_capabilities() -> Dict[str, Any]:
    """返回当前环境的输出能力，供 demo 与文档如实播报。

    返回:
        字典，含 ``pillow``（是否可用）、``pillow_error``、``cjk_font``（字体路径或 ``None``）、
        ``expected_format``（``"png"`` 或 ``"svg"``）、``degrade_reason``。
    """
    font_path = find_cjk_font()
    if not pillow_available():
        return {
            "pillow": False,
            "pillow_error": PIL_IMPORT_ERROR,
            "cjk_font": font_path,
            "expected_format": "svg",
            "degrade_reason": DEGRADE_MESSAGES["no_pillow"],
        }
    if not font_path:
        return {
            "pillow": True,
            "pillow_error": None,
            "cjk_font": None,
            "expected_format": "svg",
            "degrade_reason": DEGRADE_MESSAGES["no_cjk_font"],
        }
    return {
        "pillow": True,
        "pillow_error": None,
        "cjk_font": font_path,
        "expected_format": "png",
        "degrade_reason": None,
    }
