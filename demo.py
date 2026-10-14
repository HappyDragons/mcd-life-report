"""麦门人生报告 · 离线命令行 demo。

**零 Token、零联网、零第三方依赖**即可跑通完整链路：

::

    python demo.py                    # 用仓库内置的合成示例数据
    python demo.py --backend svg      # 强制输出 SVG 长图
    python demo.py --json             # 额外打印机器可读的完整报告 JSON
    python demo.py --input my.json    # 用你自己从 MCP 导出的 order-list 结果

流程：读订单 → 适配（字段映射 / 金额换算 / 退款过滤）→ 人生分期 → 里程碑提取
→ 转折点标注 → 人生旁白 → 渲染 1200×2200 分享长图。

本文件同时导出 :func:`build_report`，供 `src/card.py` 与 `tests/` 复用：
它把「订单 + 适配元信息」编译成一份纯数据报告，渲染层只消费这份报告。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

if __package__ in (None, ""):  # 支持 `python demo.py` 直接执行
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import card as card_mod
from src import chronicle as chronicle_mod
from src import milestones as milestones_mod
from src import narrative as narrative_mod
from src.mcd_adapter import (
    MCDAdapterError,
    ORDER_LIST_WINDOW,
    Order,
    adapt_orders,
    format_yuan,
)

DEFAULT_INPUT = os.path.join("fixtures", "orders.json")
DEFAULT_OUT_DIR = "out"
DEFAULT_BASENAME = "mcd-life-report"

BACKEND_CHOICES = ("auto", "png", "svg")

#: 数据来源标注。合成数据必须在这里被明确标记，避免读者误以为是实时结果。
SYNTHETIC_NOTICE = (
    "本文件是人工虚构的合成示例数据，用于离线演示与测试；不是真实用户订单，也不是实时的 MCP 返回结果。"
)


# --------------------------------------------------------------------------- #
# 报告编译
# --------------------------------------------------------------------------- #


def _offset_days(order: Order, origin: Optional[datetime]) -> Optional[float]:
    """计算订单相对窗口起点的天数偏移。"""
    if order.created_at is None or origin is None:
        return None
    return (order.created_at - origin).total_seconds() / 86400.0


def build_report(
    orders: Sequence[Order],
    adapter_meta: Dict[str, Any],
    source_label: str,
    source_notice: str,
    synthetic: bool,
    generated_at: Optional[str] = None,
) -> Dict[str, Any]:
    """把订单序列编译成完整报告（纯数据，可 JSON 序列化）。

    参数:
        orders: 已适配的订单序列（升序）。
        adapter_meta: ``mcd_adapter.adapt_orders`` 返回的元信息。
        source_label: 数据来源的人类可读标注，例如 ``"fixtures/orders.json（合成示例数据）"``。
        source_notice: 来源声明的长文本（会出现在长图页脚）。
        synthetic: 是否为合成数据。为 ``True`` 时报告里会有醒目的合成标记。
        generated_at: 生成时间文本；``None`` 时取当前时间。

    返回:
        报告字典，字段：``generated_at`` / ``source`` / ``window`` / ``totals`` / ``phases`` /
        ``milestones`` / ``timeline`` / ``narrative`` / ``data_quality``。

    异常:
        chronicle.ChronicleError: 订单为空时抛出。
        narrative.NarrativeError: 无法生成旁白时抛出。
    """
    chronicle = chronicle_mod.classify_phases(orders)
    milestone_map = milestones_mod.extract_milestones(orders)
    chronicle.turn_points = chronicle_mod.compute_turn_points(orders, chronicle, milestone_map)
    timeline = chronicle_mod.build_timeline(orders, chronicle)

    counted = [o for o in orders if o.counted_in_spend]
    refunded = [o for o in orders if o.is_fully_refunded]
    timed = [o for o in orders if o.created_at is not None]
    origin = timed[0].created_at if timed else None
    span_days = chronicle_mod.window_span_days(orders)

    gross = sum(o.amount_cents for o in orders)
    refund_total = sum(o.refund_cents for o in orders)
    net = sum(o.net_cents for o in counted)

    missing_summary = dict(adapter_meta.get("missing_field_summary", {}))
    warnings = list(chronicle.warnings)
    if adapter_meta.get("duplicated"):
        warnings.append("适配层忽略了 %d 条重复订单（order_id 相同）。" % adapter_meta["duplicated"])
    if adapter_meta.get("dropped_no_time"):
        warnings.append("有 %d 单无法解析下单时间。" % adapter_meta["dropped_no_time"])
    status_refunds = [o for o in orders if o.refund_source == "status"]
    if status_refunds:
        warnings.append(
            "有 %d 单没有显式退款金额字段，是按状态文本（含退款/取消关键词）判定为整单退款的，"
            "这条判据属于兜底逻辑，建议自行核对。" % len(status_refunds)
        )
    if missing_summary:
        warnings.append(
            "缺失字段统计：%s（缺失即标注为「未核实」，不做任何猜测填充）"
            % "、".join("%s × %d" % (k, v) for k, v in sorted(missing_summary.items()))
        )

    narrative = narrative_mod.build_narrative(
        orders, chronicle, milestone_map, window_span=span_days, data_notes=None
    )

    phase_segments: List[Dict[str, Any]] = []
    for segment in chronicle.segments:
        phase_segments.append(
            {
                "phase": segment.phase,
                "label": segment.label,
                "order_count": segment.order_count,
                "spend_cents": segment.spend_cents,
                "span_days": round(segment.span_days, 2),
                "order_ids": list(segment.order_ids),
                "start_offset": _offset_days(orders[segment.start_index], origin),
                "end_offset": _offset_days(orders[segment.end_index], origin),
            }
        )

    milestone_list = milestones_mod.milestone_table(milestone_map)

    return {
        "generated_at": generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": {
            "label": source_label,
            "notice": source_notice,
            "synthetic": bool(synthetic),
            "mcp_tools": ["order-list", "query-my-account", "query-my-coupons", "now-time-info"],
            "read_only": True,
        },
        "window": {
            "order_count": len(orders),
            "counted_orders": len(counted),
            "refunded_orders": len(refunded),
            "first_at": timed[0].created_at.strftime("%Y-%m-%d %H:%M") if timed else None,
            "last_at": timed[-1].created_at.strftime("%Y-%m-%d %H:%M") if timed else None,
            "span_days": span_days,
            "mcp_window": ORDER_LIST_WINDOW,
            "may_be_truncated": bool(adapter_meta.get("may_be_truncated")),
        },
        "totals": {
            "gross_cents": gross,
            "refunded_cents": refund_total,
            "net_cents": net,
            "avg_ticket_cents": milestones_mod.average_ticket_cents(orders),
        },
        "phases": {
            "flow": chronicle_mod.summarise_phase_flow(chronicle),
            "counts": chronicle.phase_counts(),
            "median_gap_days": chronicle.median_gap_days,
            "median_amount_cents": chronicle.median_amount_cents,
            "category_signal": chronicle.category_signal,
            "segments": phase_segments,
            "criteria": chronicle_mod.describe_phase_criteria(),
            "per_order": [
                {
                    "index": index,
                    "order_id": orders[index].order_id,
                    "at": orders[index].created_at.isoformat(sep=" ") if orders[index].created_at else None,
                    "phase": chronicle.phases[index],
                    "reason": chronicle.reasons[index],
                    "gap_days": chronicle.gaps_days[index],
                }
                for index in range(len(orders))
            ],
        },
        "milestones": milestone_list,
        "milestone_criteria": milestones_mod.describe_milestone_criteria(),
        "narrative_rules": narrative_mod.describe_narrative_rules(),
        "timeline": [
            {
                "index": entry["index"],
                "order_id": entry["order_id"],
                "at": entry["at"],
                "day_offset": entry["day_offset"],
                "amount_cents": entry["amount_cents"],
                "net_cents": entry["net_cents"],
                "slot": entry["slot"],
                "phase": entry["phase"],
                "phase_reason": entry["phase_reason"],
                "refunded": entry["refunded"],
                "is_origin": entry["is_origin"],
                "is_turn_point": entry["is_turn_point"],
                "turn_reasons": list(entry["turn_reasons"]),
                "top_items": entry["top_items"],
                "quantity": entry["quantity"],
            }
            for entry in timeline
        ],
        "turn_points": [
            {
                "index": point.index,
                "order_id": point.order_id,
                "at": point.at.isoformat(sep=" ") if point.at else None,
                "reasons": list(point.reasons),
            }
            for point in chronicle.turn_points
        ],
        "narrative": narrative,
        "data_quality": {
            "warnings": warnings,
            "missing_field_summary": missing_summary,
            "adapter_meta": {
                "raw_count": adapter_meta.get("raw_count"),
                "duplicated": adapter_meta.get("duplicated"),
                "amount_unit": adapter_meta.get("amount_unit"),
            },
        },
    }


# --------------------------------------------------------------------------- #
# 终端输出
# --------------------------------------------------------------------------- #

RULE = "=" * 78
SUB_RULE = "-" * 78


def _print_header() -> None:
    """打印终端报告头。"""
    print(RULE)
    print("  麦门人生报告 · mcd-life-report")
    print("  别人问你喜欢吃什么；我们问你这些年到底吃了什么。")
    print("  你的点餐史，就是你的人生编年史。")
    print(RULE)


def _print_source(report: Dict[str, Any]) -> None:
    """打印数据来源区块。"""
    source = report["source"]
    window = report["window"]
    print()
    print("[数据来源]")
    print("  文件/来源：%s" % source["label"])
    print("  合成标记：%s" % ("是（合成示例数据，非真实订单）" if source["synthetic"] else "否"))
    print("  使用工具：%s（只读，不下单、不领券）" % " / ".join(source["mcp_tools"]))
    print("  窗口限制：MCP order-list 无分页，最多返回最近 %d 单；本次 %d 单%s" % (
        window["mcp_window"],
        window["order_count"],
        "（窗口已填满，可能被截断）" if window["may_be_truncated"] else "",
    ))
    print("  说明：%s" % source["notice"])


def _print_window(report: Dict[str, Any]) -> None:
    """打印窗口概览区块。"""
    window = report["window"]
    totals = report["totals"]
    print()
    print("[窗口概览]")
    print("  订单数：%d 单（%d 单计入消费，%d 单整单退款已剔除）"
          % (window["order_count"], window["counted_orders"], window["refunded_orders"]))
    print("  时间范围：%s ~ %s（跨 %s）" % (
        window["first_at"] or "未核实",
        window["last_at"] or "未核实",
        ("%.0f 天" % window["span_days"]) if window["span_days"] is not None else "未核实",
    ))
    print("  累计消费：%s（实付 %s − 退款 %s）" % (
        format_yuan(totals["net_cents"]),
        format_yuan(totals["gross_cents"]),
        format_yuan(totals["refunded_cents"]),
    ))
    print("  平均客单价：%s" % (
        format_yuan(totals["avg_ticket_cents"]) if totals["avg_ticket_cents"] is not None else "未核实"
    ))


def _print_phases(report: Dict[str, Any]) -> None:
    """打印分期区块（含每一单的判据）。"""
    phases = report["phases"]
    print()
    print("[人生分期]")
    print("  阶段流水：%s" % phases["flow"])
    print("  单数分布：%s" % "、".join("%s %d 单" % (k, v) for k, v in phases["counts"].items()))
    print("  中位节奏：%s ｜ 中位客单价：%s ｜ 探索期信号源：%s" % (
        ("%.1f 天" % phases["median_gap_days"]) if phases["median_gap_days"] is not None else "未核实",
        format_yuan(phases["median_amount_cents"]) if phases["median_amount_cents"] is not None else "未核实",
        {"category": "商品品类", "item_name": "商品名（降级）", "none": "无（判据不生效）"}.get(
            phases["category_signal"], phases["category_signal"]
        ),
    ))
    print("  片段：")
    for index, segment in enumerate(phases["segments"], start=1):
        print("   %2d) %-4s %s  %d 单  %s"
              % (index, segment["phase"], segment["label"], segment["order_count"], format_yuan(segment["spend_cents"])))
    print("  逐单判据：")
    for entry in phases["per_order"]:
        gap = ("距上一单 %.1f 天" % entry["gap_days"]) if entry["gap_days"] is not None else "首单"
        print("   %2d. %s  %-4s  %s（%s）"
              % (entry["index"] + 1, entry["at"] or "时间未核实", entry["phase"], entry["reason"], gap))


def _print_milestones(report: Dict[str, Any]) -> None:
    """打印里程碑区块。"""
    print()
    print("[六条里程碑]")
    for milestone in report["milestones"]:
        flag = "" if milestone["available"] else "  ← 未核实"
        print("  %-8s %-18s %s" % (milestone["title"], milestone["display"], milestone["detail"] + flag))


def _print_turn_points(report: Dict[str, Any]) -> None:
    """打印转折点区块。"""
    print()
    print("[人生转折点]")
    points = report["turn_points"]
    if not points:
        print("  窗口内没有触发转折点判据的订单。")
        return
    for index, point in enumerate(points, start=1):
        print("  %d. %s｜%s" % (index, point["at"] or "时间未核实", "；".join(point["reasons"])))


def _print_narrative(report: Dict[str, Any]) -> None:
    """打印人生旁白区块。"""
    narrative = report["narrative"]
    print()
    print("[人生旁白]")
    for paragraph in narrative["paragraphs"]:
        print("  " + paragraph)
    print()
    print("  ▶ %s" % narrative["verdict"])
    print("  标签：%s" % " · ".join(narrative["tags"]))


def _print_honesty(report: Dict[str, Any]) -> None:
    """打印诚实性说明区块。"""
    print()
    print("[诚实性说明与已知限制]")
    for note in report["narrative"]["honesty"]:
        print("  · %s" % note.replace("**", ""))
    for warning in report["data_quality"]["warnings"]:
        print("  · %s" % warning)


def _print_criteria(report: Dict[str, Any]) -> None:
    """打印口径说明区块（分期 / 里程碑 / 旁白规则）。"""
    print()
    print("[口径说明]（全部为确定性规则，详见 README）")
    for item in report["phases"]["criteria"]:
        print("  分期·%-4s %s ｜ 理由：%s" % (item["phase"], item["criteria"], item["why"]))
    for item in report["milestone_criteria"]:
        print("  里程碑·%-8s %s" % (item["title"], item["criteria"]))
    for item in report["narrative_rules"]:
        print("  旁白·%-6s %s" % (item["dimension"], item["rule"]))


def _print_card(result: card_mod.RenderResult, capabilities: Dict[str, Any]) -> None:
    """打印长图输出结果（含降级说明）。"""
    print()
    print("[分享长图]")
    print("  期望格式：%s（Pillow：%s；中文字体：%s）"
          % (
              capabilities["expected_format"].upper(),
              "可用" if capabilities["pillow"] else "不可用",
              capabilities["cjk_font"] or "未找到",
          ))
    for line in result.notice().splitlines():
        print("  " + line)
    if result.degraded:
        print("  提示：SVG 是自包含矢量长图（1200×2200），浏览器/微信内直接打开即可分享。")
    print("  输出文件：%s" % result.path)


def print_text_report(report: Dict[str, Any], result: card_mod.RenderResult, capabilities: Dict[str, Any]) -> None:
    """按区块打印完整的中文文字报告。

    参数:
        report: :func:`build_report` 的输出。
        result: 长图渲染结果。
        capabilities: ``card.describe_output_capabilities()`` 的输出。
    """
    _print_header()
    _print_source(report)
    _print_window(report)
    _print_phases(report)
    _print_milestones(report)
    _print_turn_points(report)
    _print_narrative(report)
    _print_card(result, capabilities)
    _print_honesty(report)
    _print_criteria(report)
    print()
    print(RULE)
    print("  报告结束。全部结论来自订单数据，由确定性规则推出：无随机数、无大模型撰写。")
    print(RULE)


# --------------------------------------------------------------------------- #
# 命令行入口
# --------------------------------------------------------------------------- #


def load_payload(path: str) -> Dict[str, Any]:
    """读取订单 JSON 文件，返回 ``(payload, 是否为合成数据, 来源说明)`` 所需的信息。

    参数:
        path: JSON 路径。

    返回:
        ``(payload, synthetic, notice)`` 三元组：``synthetic`` 取自文件内 ``_meta.synthetic``，
        缺失时按 ``False`` 处理并给出「未标注来源」的说明（宁可说不确定，也不假装确定）。
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        raise MCDAdapterError("找不到订单数据文件：%s" % path)
    except json.JSONDecodeError as exc:
        raise MCDAdapterError("订单数据文件不是合法 JSON：%s（%s）" % (path, exc))

    meta = payload.get("_meta") if isinstance(payload, dict) else None
    if isinstance(meta, dict):
        synthetic = bool(meta.get("synthetic"))
        notice = str(meta.get("notice") or meta.get("label") or "来源未标注说明。")
    else:
        synthetic = False
        notice = "该文件未提供 _meta 说明，来源未经核实；请自行确认数据出处与合规性。"
    return payload, synthetic, notice


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行主函数。

    参数:
        argv: 参数列表（不含程序名）；``None`` 时取 ``sys.argv[1:]``。

    返回:
        进程退出码：成功为 0；参数或数据出错为 2。
    """
    parser = argparse.ArgumentParser(
        prog="demo.py",
        description="麦门人生报告离线 demo：把最近 10 单订单变成一份人生编年史长图。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python demo.py\n"
            "  python demo.py --input fixtures/orders.json --out out\n"
            "  python demo.py --backend svg\n"
            "  python demo.py --json > report.json\n"
        ),
    )
    parser.add_argument("--input", default=DEFAULT_INPUT, help="订单 JSON 路径（默认 fixtures/orders.json）")
    parser.add_argument("--out", default=DEFAULT_OUT_DIR, help="长图输出目录（默认 out/）")
    parser.add_argument("--basename", default=DEFAULT_BASENAME, help="输出文件名前缀（默认 mcd-life-report）")
    parser.add_argument("--backend", choices=BACKEND_CHOICES, default="auto", help="长图后端：auto/png/svg（默认 auto）")
    parser.add_argument("--json", action="store_true", help="额外输出机器可读的完整报告 JSON")
    parser.add_argument("--quiet", action="store_true", help="只输出长图结果，不打印文字报告")
    args = parser.parse_args(argv)

    # 用默认输入时，允许在仓库外的任意目录执行（回退到脚本所在目录下的 fixtures/）。
    if args.input == DEFAULT_INPUT and not os.path.isfile(args.input):
        beside_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), DEFAULT_INPUT)
        if os.path.isfile(beside_script):
            args.input = beside_script

    if not os.path.isfile(args.input):
        print("[错误] 找不到订单数据文件：%s" % args.input, file=sys.stderr)
        print("       若你在仓库根目录运行，可直接用：python demo.py（默认读取 fixtures/orders.json）", file=sys.stderr)
        return 2

    try:
        payload, synthetic, notice = load_payload(args.input)
        orders, adapter_meta = adapt_orders(payload)
    except MCDAdapterError as exc:
        print("[错误] 订单数据适配失败：%s" % exc, file=sys.stderr)
        return 2

    if not orders:
        print("[错误] 订单窗口为空，无法生成报告。", file=sys.stderr)
        return 2

    source_label = "%s（%s）" % (args.input, "合成示例数据，非真实订单" if synthetic else "来源未核实")
    report = build_report(
        orders,
        adapter_meta,
        source_label=source_label,
        source_notice=notice,
        synthetic=synthetic,
    )

    capabilities = card_mod.describe_output_capabilities()
    force_backend = None if args.backend == "auto" else args.backend
    result = card_mod.render_card(report, args.out, basename=args.basename, force_backend=force_backend)

    if args.quiet:
        print(result.notice())
    else:
        print_text_report(report, result, capabilities)

    if args.json:
        print()
        print(json.dumps(report, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
