"""``narrative`` 单元测试：确定性、阈值边界、以及「数据缺失时说未核实」。

最重要的一条是 :meth:`DeterminismTests.test_same_input_same_text`：
同一份订单跑两次，产出的旁白必须**逐字相同**。这正是「禁止随机数、禁止大模型编造」
这条任务要求的可执行版本。
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import unittest
from datetime import datetime, timedelta
from typing import List, Optional, Sequence

from src import narrative as narrative_mod
from src.chronicle import classify_phases
from src.mcd_adapter import Order, OrderItem
from src.milestones import extract_milestones
from src.narrative import (
    build_narrative,
    item_dependency,
    slot_distribution,
    spend_trend,
)

BASE = datetime(2026, 1, 1, 6, 0, 0)


def at(days: float, hour: int = 12, minute: int = 0) -> datetime:
    """构造相对基准时间的时刻。"""
    return BASE + timedelta(days=days, hours=hour - 6, minutes=minute)


def item(name: str = "巨无霸", quantity: int = 1, price: int = 2500, category: str = "汉堡") -> OrderItem:
    """构造一个商品行。"""
    return OrderItem(name=name, quantity=quantity, unit_price_cents=price, category=category)


def order(
    index: int,
    when: Optional[datetime],
    amount: int = 3000,
    refund: int = 0,
    items: Optional[Sequence[OrderItem]] = None,
) -> Order:
    """构造一笔订单。"""
    return Order(
        order_id="N-%02d" % index,
        created_at=when,
        amount_cents=amount,
        refund_cents=refund,
        store_name="示例门店N",
        items=list(items) if items is not None else [item()],
    )


def slots_at(hours: Sequence[int]) -> List[Order]:
    """按给定小时构造一批订单（金额相同）。"""
    return [order(index, at(float(index), hour=hour)) for index, hour in enumerate(hours)]


class SlotDistributionTests(unittest.TestCase):
    """时段画像阈值。"""

    def test_dominant_slot_at_ratio_boundary(self) -> None:
        result = slot_distribution(slots_at([12, 13, 18, 19, 23]))
        self.assertEqual(result["dominant"], "午市")
        self.assertAlmostEqual(result["dominant_ratio"] or 0.0, 0.4, places=6)

    def test_tie_is_broken_deterministically(self) -> None:
        # 早晨与午市各 2 单（合计 5 单 → 占比 40%，达到主时段门槛），平局时取更靠前的时段
        result = slot_distribution(slots_at([8, 9, 12, 13, 18]))
        self.assertEqual(result["counts"]["早晨"], 2)
        self.assertEqual(result["counts"]["午市"], 2)
        self.assertEqual(result["dominant"], "早晨")

    def test_tie_below_threshold_is_scattered(self) -> None:
        result = slot_distribution(slots_at([8, 12, 18, 23]))
        self.assertEqual(result["dominant"], "时段分散型")

    def test_below_ratio_is_scattered(self) -> None:
        result = slot_distribution(slots_at([8, 9, 10, 12, 13, 18, 19, 20, 23]))
        self.assertEqual(result["dominant"], "时段分散型")

    def test_night_ratio_boundary(self) -> None:
        # 10 单里 3 单落在深夜 → 恰好 30%，记为夜行倾向
        result = slot_distribution(slots_at([12, 12, 18, 18, 14, 14, 16, 23, 22, 1]))
        self.assertAlmostEqual(result["night_ratio"] or 0.0, 0.3, places=6)
        self.assertTrue(result["has_night_bias"])

    def test_night_ratio_below_boundary(self) -> None:
        # 10 单里 2 单落在深夜 → 20%，不算夜行
        result = slot_distribution(slots_at([12, 12, 18, 18, 14, 14, 16, 17, 23, 1]))
        self.assertAlmostEqual(result["night_ratio"] or 0.0, 0.2, places=6)
        self.assertFalse(result["has_night_bias"])

    def test_refunded_and_untimed_excluded_from_denominator(self) -> None:
        orders = slots_at([12, 13])
        orders.append(order(9, at(9), amount=3000, refund=3000))
        orders.append(order(10, at(10, hour=20), items=[item()]))
        orders[-1].created_at = None
        result = slot_distribution(orders)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["skipped"], 1)

    def test_no_data(self) -> None:
        result = slot_distribution([order(0, None)])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["dominant"], "时段未核实")
        self.assertIsNone(result["night_ratio"])


class ItemDependencyTests(unittest.TestCase):
    """单品依赖度分级。"""

    def test_heavy_at_thirty_percent(self) -> None:
        orders = [order(0, at(0), items=[item("巨无霸", 3), item("薯条(中)", 7)])]
        result = item_dependency(orders, "巨无霸")
        self.assertEqual(result["level"], "重度依赖型")
        self.assertAlmostEqual(result["ratio"] or 0.0, 0.3, places=6)

    def test_repeat_between_15_and_30(self) -> None:
        orders = [order(0, at(0), items=[item("巨无霸", 2), item("薯条(中)", 8)])]
        self.assertEqual(item_dependency(orders, "巨无霸")["level"], "稳定复购型")

    def test_scattered_below_15(self) -> None:
        orders = [order(0, at(0), items=[item("巨无霸", 1), item("薯条(中)", 9)])]
        self.assertEqual(item_dependency(orders, "巨无霸")["level"], "广泛探索型")

    def test_no_top_item(self) -> None:
        result = item_dependency([order(0, at(0))], None)
        self.assertEqual(result["level"], "数据不足")
        self.assertIsNone(result["ratio"])


class SpendTrendTests(unittest.TestCase):
    """消费走势判据。"""

    def test_rising(self) -> None:
        orders = [order(0, at(0), amount=1000), order(1, at(1), amount=1000), order(2, at(2), amount=2000), order(3, at(3), amount=2000)]
        self.assertEqual(spend_trend(orders)["level"], "上升")

    def test_falling(self) -> None:
        orders = [order(0, at(0), amount=2000), order(1, at(1), amount=2000), order(2, at(2), amount=1000), order(3, at(3), amount=1000)]
        self.assertEqual(spend_trend(orders)["level"], "下降")

    def test_flat_inside_band(self) -> None:
        orders = [order(0, at(0), amount=1000), order(1, at(1), amount=1000), order(2, at(2), amount=1050), order(3, at(3), amount=1050)]
        self.assertEqual(spend_trend(orders)["level"], "平稳")

    def test_exactly_fifteen_percent_is_flat(self) -> None:
        orders = [order(0, at(0), amount=1000), order(1, at(1), amount=1000), order(2, at(2), amount=1150), order(3, at(3), amount=1150)]
        result = spend_trend(orders)
        self.assertAlmostEqual(result["change_ratio"] or 0.0, 0.15, places=6)
        self.assertEqual(result["level"], "平稳")

    def test_insufficient_data(self) -> None:
        self.assertEqual(spend_trend([order(0, at(0))])["level"], "数据不足")

    def test_split_reported(self) -> None:
        orders = [order(i, at(float(i)), amount=1000 + i) for i in range(5)]
        self.assertEqual(spend_trend(orders)["split"], 2)


class DeterminismTests(unittest.TestCase):
    """确定性与「不编造」的可执行保障。"""

    def setUp(self) -> None:
        fixture = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "orders.json")
        with open(fixture, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        from src.mcd_adapter import adapt_orders

        self.orders, _ = adapt_orders(payload)

    def _build(self):
        chronicle = classify_phases(self.orders)
        milestone_map = extract_milestones(self.orders)
        return build_narrative(self.orders, chronicle, milestone_map, window_span=159.4)

    def test_same_input_same_text(self) -> None:
        first = self._build()
        second = self._build()
        self.assertEqual(
            json.dumps(first, ensure_ascii=False, sort_keys=True),
            json.dumps(second, ensure_ascii=False, sort_keys=True),
        )

    def test_module_has_no_randomness_or_network_or_llm(self) -> None:
        """用 AST 扫描 import 与标识符，而不是扫字符串（避免误伤文档里的说明文字）。"""
        tree = ast.parse(inspect.getsource(narrative_mod))
        forbidden_modules = {
            "random",
            "secrets",
            "requests",
            "httpx",
            "urllib",
            "socket",
            "http",
            "openai",
            "anthropic",
            "numpy",
            "pandas",
        }
        imported = set()
        used_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.Name):
                used_names.add(node.id)

        self.assertEqual(imported & forbidden_modules, set(), "旁白模块引入了禁止的模块")
        self.assertNotIn("random", used_names, "旁白模块不得使用 random 标识符")
        # 只允许标准库
        self.assertTrue(imported <= {"typing", "chronicle", "mcd_adapter", "__future__"}, "出现了非预期的依赖：%s" % imported)

    def test_all_numbers_in_verdict_come_from_data(self) -> None:
        result = self._build()
        self.assertIn("10 单", result["verdict"])
        self.assertIn("¥344.50", " ".join(result["paragraphs"]))
        self.assertIn("344.50", result["verdict"] + " ".join(result["paragraphs"]))

    def test_tags_and_headline_present(self) -> None:
        result = self._build()
        self.assertEqual(len(result["tags"]), 3)
        self.assertTrue(result["headline"])
        self.assertIn("编年史", result["headline"])

    def test_honesty_mentions_window_limit(self) -> None:
        result = self._build()
        blob = " ".join(result["honesty"])
        self.assertIn("order-list", blob)
        self.assertIn("10 单", blob)

    def test_honesty_mentions_refund_when_present(self) -> None:
        blob = " ".join(self._build()["honesty"])
        self.assertIn("整单退款", blob)


class HonestyTests(unittest.TestCase):
    """数据缺失与异常输入时的诚实输出。"""

    def test_untimed_orders_do_not_crash_and_say_unverified(self) -> None:
        orders = [order(0, None, items=[item()]), order(1, at(1))]
        chronicle = classify_phases(orders)
        milestone_map = extract_milestones(orders)
        result = build_narrative(orders, chronicle, milestone_map, window_span=None)
        blob = " ".join(result["paragraphs"]) + " ".join(result["honesty"])
        self.assertIn("未核实", blob)
        self.assertTrue(any("缺少" in note for note in result["honesty"]))

    def test_no_items_gives_data_insufficient_wording(self) -> None:
        orders = [order(0, at(0), items=[]), order(1, at(30), items=[])]
        chronicle = classify_phases(orders)
        milestone_map = extract_milestones(orders)
        result = build_narrative(orders, chronicle, milestone_map, window_span=30.0)
        blob = " ".join(result["paragraphs"])
        self.assertIn("商品明细不足", blob)

    def test_empty_orders_raise(self) -> None:
        chronicle = classify_phases([order(0, at(0))])
        with self.assertRaises(narrative_mod.NarrativeError):
            build_narrative([], chronicle, {})

    def test_extra_data_notes_are_forwarded(self) -> None:
        orders = [order(0, at(0)), order(1, at(30))]
        chronicle = classify_phases(orders)
        milestone_map = extract_milestones(orders)
        result = build_narrative(
            orders, chronicle, milestone_map, window_span=30.0, data_notes=["自定义缺失说明"]
        )
        self.assertIn("自定义缺失说明", result["honesty"])

    def test_narrative_rules_documented(self) -> None:
        rules = narrative_mod.describe_narrative_rules()
        self.assertEqual(len(rules), 4)
        for entry in rules:
            self.assertTrue(entry["dimension"])
            self.assertTrue(entry["rule"])


if __name__ == "__main__":
    unittest.main()
