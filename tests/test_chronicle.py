"""``chronicle`` 单元测试：分期判据的边界值、时段划分、转折点。

判据边界是本文件的核心：所有阈值都按「刚好命中 / 刚好不命中」两侧各写一条用例，
避免出现「改了阈值测试还全绿」的假安全感。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from typing import List, Optional, Sequence

from src.chronicle import (
    DECLINE_STREAK,
    FIRST_ENCOUNTER_DAYS,
    FADE_MEDIAN_MULTIPLE,
    GAP_DORMANT_DAYS,
    GAP_FADE_DAYS,
    PHASE_BURNOUT,
    PHASE_EXPLORE,
    PHASE_FIRST,
    PHASE_REVIVAL,
    PHASE_STABLE,
    PHASE_UNKNOWN,
    Chronicle,
    ChronicleError,
    classify_phases,
    compute_turn_points,
    describe_phase_criteria,
    is_night_order,
    summarise_phase_flow,
    time_slot,
    window_span_days,
)
from src.mcd_adapter import Order, OrderItem

BASE = datetime(2026, 1, 1, 12, 0, 0)


def at(days: float, hour: int = 12, minute: int = 0) -> datetime:
    """构造相对基准时间的时刻（``days`` 支持小数，用于构造精确的间隔边界）。"""
    return BASE + timedelta(days=days, hours=hour - 12, minutes=minute)


def item(name: str = "巨无霸", quantity: int = 1, price: int = 2500, category: str = "汉堡") -> OrderItem:
    """构造一个商品行。"""
    return OrderItem(name=name, quantity=quantity, unit_price_cents=price, category=category)


def order(index: int, when: Optional[datetime], amount: int = 3000, refund: int = 0, items: Optional[Sequence[OrderItem]] = None) -> Order:
    """构造一笔订单。"""
    return Order(
        order_id="T-%02d" % index,
        created_at=when,
        amount_cents=amount,
        refund_cents=refund,
        store_name="示例门店T",
        items=list(items) if items is not None else [item()],
    )


def series(gaps: Sequence[float], amounts: Optional[Sequence[int]] = None, items_per_order=None) -> List[Order]:
    """按「相邻间隔天数」构造订单序列。

    参数:
        gaps: 第 1 单固定在第 0 天，之后每个元素是相对上一单的间隔天数。
        amounts: 各单实付金额；``None`` 时全部 3000。
        items_per_order: 各单商品；``None`` 时全部为同一款「巨无霸」。
    """
    stamps = [0.0]
    for gap in gaps:
        stamps.append(stamps[-1] + gap)
    orders = []
    for index, stamp in enumerate(stamps):
        amount = amounts[index] if amounts is not None else 3000
        its = items_per_order[index] if items_per_order is not None else None
        orders.append(order(index, at(stamp), amount=amount, items=its))
    return orders


class PhaseBoundaryTests(unittest.TestCase):
    """分期判据的边界值。"""

    def test_first_order_is_always_first_phase(self) -> None:
        chronicle = classify_phases(series([1.0]))
        self.assertEqual(chronicle.phases[0], PHASE_FIRST)

    def test_first_encounter_window_upper_boundary(self) -> None:
        # 恰好 14 天：仍在初遇期
        chronicle = classify_phases(series([FIRST_ENCOUNTER_DAYS]))
        self.assertEqual(chronicle.phases[1], PHASE_FIRST)
        self.assertIn("≤ 14 天", chronicle.reasons[1])

    def test_first_encounter_window_just_outside(self) -> None:
        # 14.01 天：离开初遇期，且无新品类 → 稳定期
        chronicle = classify_phases(series([FIRST_ENCOUNTER_DAYS + 0.01]))
        self.assertEqual(chronicle.phases[1], PHASE_STABLE)

    def test_explore_when_new_category_appears(self) -> None:
        orders = series(
            [20.0, 20.0],
            items_per_order=[
                [item(category="汉堡")],
                [item(name="巨无霸", category="汉堡")],
                [item(name="麦旋风", category="甜品")],
            ],
        )
        chronicle = classify_phases(orders)
        self.assertEqual(chronicle.phases[0], PHASE_FIRST)
        self.assertEqual(chronicle.phases[1], PHASE_STABLE)
        self.assertEqual(chronicle.phases[2], PHASE_EXPLORE)
        self.assertIn("甜品", chronicle.reasons[2])

    def test_revival_at_exactly_thirty_days(self) -> None:
        chronicle = classify_phases(series([GAP_DORMANT_DAYS, 5.0]))
        self.assertEqual(chronicle.phases[1], PHASE_REVIVAL)
        self.assertIn("回来了", chronicle.reasons[1])

    def test_no_revival_just_below_thirty_days(self) -> None:
        chronicle = classify_phases(series([GAP_DORMANT_DAYS - 0.01, 5.0]))
        self.assertEqual(chronicle.phases[1], PHASE_STABLE)

    def test_burnout_when_dormant_and_no_later_order(self) -> None:
        chronicle = classify_phases(series([1.0, GAP_DORMANT_DAYS]))
        self.assertEqual(chronicle.phases[2], PHASE_BURNOUT)
        self.assertIn("最后一单", chronicle.reasons[2])

    def test_burnout_by_relative_slowdown(self) -> None:
        # 中位节奏 1 天；第 4 单间隔 14 天（≥14 且 ≥ 2×1）→ 倦怠
        chronicle = classify_phases(series([1.0, 1.0, GAP_FADE_DAYS]))
        self.assertEqual(chronicle.phases[3], PHASE_BURNOUT)
        self.assertIn("倍", chronicle.reasons[3])

    def test_relative_slowdown_needs_absolute_floor(self) -> None:
        # 中位节奏 3 天，间隔 5 天：没到 14 天的绝对下限，不算倦怠（此时已离开初遇期）
        chronicle = classify_phases(series([3.0, 3.0, 3.0, 3.0, 5.0]))
        self.assertEqual(chronicle.phases[5], PHASE_STABLE)

    def test_relative_slowdown_double_boundary(self) -> None:
        # 中位节奏 7 天，间隔恰好 14 天（= 2 倍）→ 倦怠
        chronicle = classify_phases(series([7.0, 7.0, 7.0, FADE_MEDIAN_MULTIPLE * 7.0]))
        self.assertAlmostEqual(chronicle.median_gap_days or 0.0, 7.0, places=6)
        self.assertEqual(chronicle.phases[4], PHASE_BURNOUT)

    def test_decline_streak_triggers_burnout(self) -> None:
        chronicle = classify_phases(series([20.0, 20.0], amounts=[5000, 4000, 3000]))
        self.assertEqual(chronicle.phases[2], PHASE_BURNOUT)
        self.assertIn("递减", chronicle.reasons[2])

    def test_decline_needs_strictly_decreasing(self) -> None:
        # 金额持平：不满足「严格递减」
        chronicle = classify_phases(series([20.0, 20.0], amounts=[3000, 3000, 3000]))
        self.assertEqual(chronicle.phases[2], PHASE_STABLE)

    def test_decline_needs_enough_orders(self) -> None:
        # 只有两单递减，不足以触发（需要 DECLINE_STREAK 单）
        self.assertEqual(DECLINE_STREAK, 3)
        chronicle = classify_phases(series([20.0], amounts=[5000, 3000]))
        self.assertEqual(chronicle.phases[1], PHASE_STABLE)

    def test_decline_last_must_be_below_median(self) -> None:
        # 后三单递减，但最后一单等于中位客单价 → 不触发
        chronicle = classify_phases(series([20.0] * 4, amounts=[100, 100, 5000, 4000, 3000]))
        self.assertEqual(chronicle.phases[4], PHASE_STABLE)

    def test_decline_skips_when_refunded_order_in_window(self) -> None:
        orders = series([20.0, 20.0], amounts=[5000, 4000, 3000])
        orders[1].refund_cents = 4000  # 整单退款 → 净额 0，不应参与金额走弱判据
        chronicle = classify_phases(orders)
        self.assertEqual(chronicle.phases[2], PHASE_STABLE)

    def test_revival_takes_precedence_over_decline(self) -> None:
        # 断档 40 天后归来：即使金额也在走低，也优先判回潮
        chronicle = classify_phases(series([40.0, 5.0], amounts=[5000, 4000, 3000]))
        self.assertEqual(chronicle.phases[1], PHASE_REVIVAL)

    def test_median_gap_and_amount_are_reported(self) -> None:
        chronicle = classify_phases(series([1.0, 1.0, 1.0], amounts=[1000, 2000, 3000, 4000]))
        self.assertAlmostEqual(chronicle.median_gap_days or 0.0, 1.0, places=6)
        self.assertEqual(chronicle.median_amount_cents, 2500)

    def test_gaps_alignment(self) -> None:
        chronicle = classify_phases(series([2.0, 3.0]))
        self.assertIsNone(chronicle.gaps_days[0])
        self.assertAlmostEqual(chronicle.gaps_days[1] or 0.0, 2.0, places=6)
        self.assertAlmostEqual(chronicle.gaps_days[2] or 0.0, 3.0, places=6)

    def test_empty_series_raises(self) -> None:
        with self.assertRaises(ChronicleError):
            classify_phases([])


class SignalDegradeTests(unittest.TestCase):
    """品类信号缺失时的降级与警告（必须如实说明，不许静默改变口径）。"""

    def test_fallback_to_item_name(self) -> None:
        orders = series(
            [20.0, 20.0],
            items_per_order=[
                [item(category="未分类")],
                [item(category="未分类")],
                [item(name="麦旋风", category="未分类")],
            ],
        )
        chronicle = classify_phases(orders)
        self.assertEqual(chronicle.category_signal, "item_name")
        self.assertEqual(chronicle.phases[2], PHASE_EXPLORE)
        self.assertTrue(any("降级" in warning for warning in chronicle.warnings))

    def test_no_signal_available(self) -> None:
        blank = OrderItem(name="未标注商品", quantity=1, unit_price_cents=None, category="未分类")
        orders = series([20.0, 20.0], items_per_order=[[blank], [blank], [blank]])
        chronicle = classify_phases(orders)
        self.assertEqual(chronicle.category_signal, "none")
        self.assertNotIn(PHASE_EXPLORE, chronicle.phases)
        self.assertTrue(any("探索期" in warning for warning in chronicle.warnings))

    def test_window_warning_when_full(self) -> None:
        orders = series([1.0] * 9)
        chronicle = classify_phases(orders)
        self.assertTrue(any("最近 10 单" in warning for warning in chronicle.warnings))

    def test_small_sample_warning(self) -> None:
        chronicle = classify_phases(series([1.0]))
        self.assertTrue(any("样本过小" in warning for warning in chronicle.warnings))

    def test_untimed_order_is_unknown_and_warned(self) -> None:
        orders = [order(0, at(0)), order(1, None)]
        chronicle = classify_phases(orders)
        self.assertEqual(chronicle.phases[1], PHASE_UNKNOWN)
        self.assertTrue(any("未分期" in warning for warning in chronicle.warnings))

    def test_all_untimed(self) -> None:
        chronicle = classify_phases([order(0, None)])
        self.assertEqual(chronicle.phases[0], PHASE_UNKNOWN)
        self.assertEqual(chronicle.segments, [])


class SegmentTests(unittest.TestCase):
    """阶段片段合并。"""

    def test_segments_merge_adjacent_same_phase(self) -> None:
        orders = series([1.0, 1.0, 1.0])
        chronicle = classify_phases(orders)
        self.assertEqual(len(chronicle.segments), 1)
        segment = chronicle.segments[0]
        self.assertEqual(segment.phase, PHASE_FIRST)
        self.assertEqual(segment.order_count, 4)
        self.assertEqual(segment.start_index, 0)
        self.assertEqual(segment.end_index, 3)
        self.assertEqual(segment.spend_cents, 12000)
        self.assertIn("~", segment.label)

    def test_segment_spend_excludes_fully_refunded(self) -> None:
        orders = series([1.0, 1.0], amounts=[3000, 3000, 3000])
        orders[1].refund_cents = 3000
        chronicle = classify_phases(orders)
        self.assertEqual(chronicle.segments[0].spend_cents, 6000)

    def test_phase_counts(self) -> None:
        orders = series([1.0, 20.0])
        chronicle = classify_phases(orders)
        counts = chronicle.phase_counts()
        self.assertEqual(sum(counts.values()), 3)
        self.assertEqual(counts[PHASE_FIRST], 2)

    def test_summarise_phase_flow_dedupes(self) -> None:
        orders = series([1.0, 20.0])
        chronicle = classify_phases(orders)
        self.assertEqual(summarise_phase_flow(chronicle), "%s → %s" % (PHASE_FIRST, PHASE_STABLE))

    def test_summarise_empty(self) -> None:
        chronicle = Chronicle()
        self.assertEqual(summarise_phase_flow(chronicle), PHASE_UNKNOWN)


class TurnPointTests(unittest.TestCase):
    """转折点判定。"""

    def test_revival_entry_is_turn_point(self) -> None:
        orders = series([GAP_DORMANT_DAYS, 5.0])
        chronicle = classify_phases(orders)
        points = compute_turn_points(orders, chronicle)
        self.assertEqual([p.index for p in points], [1])
        self.assertTrue(any(PHASE_REVIVAL in reason for reason in points[0].reasons))

    def test_no_turn_points_in_flat_history(self) -> None:
        orders = series([1.0, 1.0])
        chronicle = classify_phases(orders)
        self.assertEqual(compute_turn_points(orders, chronicle), [])

    def test_milestone_driven_turn_points(self) -> None:
        from src import milestones

        orders = series([1.0, 1.0, 1.0], amounts=[3000, 3000, 9000, 3000])
        chronicle = classify_phases(orders)
        milestone_map = milestones.extract_milestones(orders)
        points = compute_turn_points(orders, chronicle, milestone_map)
        self.assertEqual([p.index for p in points], [2])
        self.assertTrue(any("最大金额" in reason for reason in points[0].reasons))


class TimeSlotTests(unittest.TestCase):
    """时段划分与深夜判定的边界。"""

    def test_slot_boundaries(self) -> None:
        cases = {
            5: "早晨",
            6: "早晨",
            10: "早晨",
            11: "午市",
            13: "午市",
            14: "下午茶",
            16: "下午茶",
            17: "晚市",
            21: "晚市",
            22: "深夜",
            23: "深夜",
            0: "深夜",
            4: "深夜",
        }
        for hour, expected in cases.items():
            self.assertEqual(time_slot(datetime(2026, 1, 1, hour, 0)), expected, "hour=%d" % hour)

    def test_minute_level_boundaries(self) -> None:
        self.assertEqual(time_slot(datetime(2026, 1, 1, 13, 59)), "午市")
        self.assertEqual(time_slot(datetime(2026, 1, 1, 14, 0)), "下午茶")
        self.assertEqual(time_slot(datetime(2026, 1, 1, 21, 59)), "晚市")
        self.assertEqual(time_slot(datetime(2026, 1, 1, 22, 0)), "深夜")
        self.assertEqual(time_slot(datetime(2026, 1, 1, 4, 59)), "深夜")
        self.assertEqual(time_slot(datetime(2026, 1, 1, 5, 0)), "早晨")

    def test_is_night_order(self) -> None:
        self.assertTrue(is_night_order(datetime(2026, 1, 1, 22, 0)))
        self.assertTrue(is_night_order(datetime(2026, 1, 1, 0, 30)))
        self.assertFalse(is_night_order(datetime(2026, 1, 1, 5, 0)))
        self.assertFalse(is_night_order(datetime(2026, 1, 1, 21, 59)))

    def test_window_span_days(self) -> None:
        orders = series([10.0, 5.0])
        self.assertAlmostEqual(window_span_days(orders) or 0.0, 15.0, places=6)
        self.assertIsNone(window_span_days([order(0, at(0))]))
        self.assertIsNone(window_span_days([]))


class CriteriaDocumentationTests(unittest.TestCase):
    """判据说明必须可读且覆盖全部五个阶段。"""

    def test_all_five_phases_documented(self) -> None:
        criteria = describe_phase_criteria()
        phases = [entry["phase"] for entry in criteria]
        self.assertEqual(
            phases, [PHASE_FIRST, PHASE_EXPLORE, PHASE_STABLE, PHASE_BURNOUT, PHASE_REVIVAL]
        )

    def test_every_criterion_has_reason(self) -> None:
        for entry in describe_phase_criteria():
            self.assertTrue(entry["criteria"].strip())
            self.assertTrue(entry["why"].strip())

    def test_thresholds_appear_in_text(self) -> None:
        blob = " ".join(entry["criteria"] for entry in describe_phase_criteria())
        self.assertIn("14", blob)
        self.assertIn("30", blob)
        self.assertIn(str(DECLINE_STREAK), blob)


if __name__ == "__main__":
    unittest.main()
