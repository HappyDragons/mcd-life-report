"""``milestones`` 单元测试：六条里程碑的口径与并列规则。

重点覆盖：

- 第一次点单是否把「已退款」也算作「点过」；
- 最长连续天数是否排除整单退款订单、同日多单是否只算一天；
- 单笔最大金额 / 最贵深夜订单的并列时取更早；
- 深夜边界（22:00 命中、04:59 命中、05:00 不命中、21:59 不命中）；
- 累计消费与平均客单价是否严格排除整单退款；
- 空窗口 / 全退款窗口必须返回「未核实」而不是 0。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from typing import Optional, Sequence

from src.mcd_adapter import Order, OrderItem
from src.milestones import (
    average_ticket_cents,
    describe_milestone_criteria,
    effective_order_count,
    extract_milestones,
    longest_streak,
    milestone_table,
    most_ordered_item,
)

BASE = datetime(2026, 1, 1, 12, 0, 0)


def at(days: float, hour: int = 12, minute: int = 0) -> datetime:
    """构造相对基准时间的时刻。"""
    return BASE + timedelta(days=days, hours=hour - 12, minutes=minute)


def item(name: str = "巨无霸", quantity: int = 1, price: int = 2500, category: str = "汉堡") -> OrderItem:
    """构造一个商品行。"""
    return OrderItem(name=name, quantity=quantity, unit_price_cents=price, category=category)


def order(
    index: int,
    when: Optional[datetime],
    amount: int = 3000,
    refund: int = 0,
    items: Optional[Sequence[OrderItem]] = None,
    store: Optional[str] = "示例门店T",
) -> Order:
    """构造一笔订单。"""
    return Order(
        order_id="M-%02d" % index,
        created_at=when,
        amount_cents=amount,
        refund_cents=refund,
        store_name=store,
        items=list(items) if items is not None else [item()],
    )


class FirstOrderTests(unittest.TestCase):
    """第一次点单。"""

    def test_earliest_order_is_first_order(self) -> None:
        orders = [order(1, at(5)), order(0, at(0))]
        result = extract_milestones(sorted(orders, key=lambda o: o.created_at))
        self.assertIs(result["first_order"].value, result["first_order"].value)
        self.assertTrue(result["first_order"].display.startswith("2026-01-01"))

    def test_refunded_first_order_still_counts_as_ordered(self) -> None:
        orders = [order(0, at(0), amount=3000, refund=3000), order(1, at(2))]
        result = extract_milestones(orders)
        self.assertTrue(result["first_order"].available)
        self.assertIn("整单退款", result["first_order"].detail)

    def test_first_order_unavailable_without_time(self) -> None:
        result = extract_milestones([order(0, None)])
        self.assertFalse(result["first_order"].available)
        self.assertEqual(result["first_order"].display, "未核实")


class StreakTests(unittest.TestCase):
    """最长连续天数。"""

    def test_consecutive_days(self) -> None:
        orders = [order(0, at(0)), order(1, at(1)), order(2, at(2)), order(3, at(10))]
        self.assertEqual(longest_streak(orders)["days"], 3)

    def test_same_day_multiple_orders_count_once(self) -> None:
        orders = [order(0, at(0, hour=12)), order(1, at(0, hour=20)), order(2, at(1))]
        self.assertEqual(longest_streak(orders)["days"], 2)

    def test_refunded_order_breaks_streak(self) -> None:
        orders = [order(0, at(0)), order(1, at(1), refund=3000), order(2, at(2))]
        streak = longest_streak(orders)
        self.assertEqual(streak["days"], 1)
        self.assertEqual(streak["dates"], ["2026-01-01"])

    def test_month_boundary(self) -> None:
        orders = [order(0, at(30, hour=23)), order(1, at(31, hour=1))]
        self.assertEqual(longest_streak(orders)["days"], 2)

    def test_empty_returns_zero(self) -> None:
        self.assertEqual(longest_streak([])["days"], 0)

    def test_milestone_display(self) -> None:
        result = extract_milestones([order(0, at(0)), order(1, at(1))])
        self.assertEqual(result["longest_streak"].display, "2 天")


class BiggestOrderTests(unittest.TestCase):
    """单笔最大金额。"""

    def test_max_amount(self) -> None:
        orders = [order(0, at(0), amount=3000), order(1, at(2), amount=9000), order(2, at(4), amount=5000)]
        result = extract_milestones(orders)
        self.assertEqual(result["biggest_order"].value, 9000)
        self.assertEqual(result["biggest_order"].display, "¥90.00")
        self.assertEqual(result["biggest_order"].order_index, 1)

    def test_tie_takes_earlier_order(self) -> None:
        orders = [order(0, at(0), amount=5000), order(1, at(2), amount=5000)]
        result = extract_milestones(orders)
        self.assertEqual(result["biggest_order"].order_index, 0)

    def test_refunded_order_excluded(self) -> None:
        orders = [order(0, at(0), amount=9000, refund=9000), order(1, at(2), amount=4000)]
        result = extract_milestones(orders)
        self.assertEqual(result["biggest_order"].value, 4000)
        self.assertEqual(result["biggest_order"].order_index, 1)

    def test_all_refunded_unavailable(self) -> None:
        orders = [order(0, at(0), amount=9000, refund=9000)]
        result = extract_milestones(orders)
        self.assertFalse(result["biggest_order"].available)
        self.assertEqual(result["biggest_order"].display, "未核实")
        self.assertIn("整单退款", result["biggest_order"].detail)


class NightOrderTests(unittest.TestCase):
    """最贵的深夜订单（22:00–04:59 边界）。"""

    def test_night_boundaries(self) -> None:
        orders = [
            order(0, at(0, hour=22, minute=0), amount=1000),
            order(1, at(1, hour=4, minute=59), amount=2000),
            order(2, at(2, hour=5, minute=0), amount=9000),
            order(3, at(3, hour=21, minute=59), amount=9000),
        ]
        result = extract_milestones(orders)
        self.assertTrue(result["priciest_night_order"].available)
        self.assertEqual(result["priciest_night_order"].value, 2000)
        self.assertEqual(result["priciest_night_order"].order_index, 1)
        self.assertIn("22:00–04:59", result["priciest_night_order"].detail)

    def test_no_night_order_is_unavailable(self) -> None:
        orders = [order(0, at(0, hour=12)), order(1, at(1, hour=18))]
        result = extract_milestones(orders)
        self.assertFalse(result["priciest_night_order"].available)
        self.assertIn("没有", result["priciest_night_order"].detail)

    def test_refunded_night_order_excluded(self) -> None:
        orders = [
            order(0, at(0, hour=23), amount=9000, refund=9000),
            order(1, at(1, hour=23), amount=1000),
        ]
        result = extract_milestones(orders)
        self.assertEqual(result["priciest_night_order"].value, 1000)

    def test_tie_takes_earlier(self) -> None:
        orders = [order(0, at(0, hour=22), amount=2000), order(1, at(1, hour=23), amount=2000)]
        result = extract_milestones(orders)
        self.assertEqual(result["priciest_night_order"].order_index, 0)


class TotalSpendTests(unittest.TestCase):
    """累计消费与平均客单价。"""

    def test_full_refund_excluded_partial_refund_netted(self) -> None:
        orders = [
            order(0, at(0), amount=3000),
            order(1, at(1), amount=4000, refund=4000),  # 整单退款：不计
            order(2, at(2), amount=5000, refund=1000),  # 部分退款：计 4000
        ]
        result = extract_milestones(orders)
        self.assertEqual(result["total_spend"].value, 7000)
        self.assertEqual(result["total_spend"].display, "¥70.00")
        self.assertIn("整单退款已剔除", result["total_spend"].detail)

    def test_gross_and_refund_reported(self) -> None:
        orders = [order(0, at(0), amount=3000), order(1, at(1), amount=2000, refund=2000)]
        detail = extract_milestones(orders)["total_spend"].detail
        self.assertIn("¥50.00", detail)
        self.assertIn("¥20.00", detail)
        self.assertIn("¥30.00", detail)

    def test_average_ticket(self) -> None:
        orders = [order(0, at(0), amount=3000), order(1, at(1), amount=2001)]
        self.assertEqual(average_ticket_cents(orders), 2501)
        self.assertIsNone(average_ticket_cents([order(0, at(0), amount=3000, refund=3000)]))

    def test_effective_order_count(self) -> None:
        orders = [order(0, at(0)), order(1, at(1), refund=3000), order(2, at(2))]
        self.assertEqual(effective_order_count(orders), 2)


class TopItemTests(unittest.TestCase):
    """最常点餐品与并列规则。"""

    def test_by_quantity(self) -> None:
        orders = [
            order(0, at(0), items=[item("巨无霸", 1), item("薯条(中)", 3)]),
            order(1, at(1), items=[item("巨无霸", 2), item("薯条(中)", 1)]),
        ]
        top = most_ordered_item(orders)
        self.assertEqual(top["name"], "薯条(中)")
        self.assertEqual(top["quantity"], 4)

    def test_tie_broken_by_order_count(self) -> None:
        orders = [
            order(0, at(0), items=[item("巨无霸", 2)]),
            order(1, at(1), items=[item("双层吉士堡", 1)]),
            order(2, at(2), items=[item("双层吉士堡", 1)]),
        ]
        top = most_ordered_item(orders)
        self.assertEqual(top["quantity"], 2)
        # 巨无霸 2 件出现在 1 单；双层吉士堡 2 件出现在 2 单 → 后者胜
        self.assertEqual(top["name"], "双层吉士堡")

    def test_tie_broken_by_first_seen_then_name(self) -> None:
        orders = [
            order(0, at(0), items=[item("A品", 1)]),
            order(1, at(1), items=[item("B品", 1)]),
        ]
        top = most_ordered_item(orders)
        self.assertEqual(top["name"], "A品")

    def test_excludes_refunded_orders(self) -> None:
        orders = [
            order(0, at(0), items=[item("巨无霸", 5)]),
            order(1, at(1), amount=3000, refund=3000, items=[item("麦旋风", 9)]),
        ]
        top = most_ordered_item(orders)
        self.assertEqual(top["name"], "巨无霸")

    def test_rank_is_deterministic(self) -> None:
        orders = [order(0, at(0), items=[item("A品", 2), item("B品", 2)])]
        top = most_ordered_item(orders)
        self.assertEqual([entry["name"] for entry in top["rank"]], ["A品", "B品"])

    def test_empty(self) -> None:
        top = most_ordered_item([])
        self.assertIsNone(top["name"])
        self.assertEqual(top["rank"], [])


class EmptyAndPartialDataTests(unittest.TestCase):
    """空窗口与数据缺失必须「未核实」，不能静默填 0。"""

    def test_empty_orders_all_unavailable(self) -> None:
        result = extract_milestones([])
        self.assertEqual(
            sorted(result.keys()),
            sorted(
                [
                    "first_order",
                    "longest_streak",
                    "biggest_order",
                    "priciest_night_order",
                    "total_spend",
                    "most_ordered_item",
                ]
            ),
        )
        for milestone in result.values():
            self.assertFalse(milestone.available)
            self.assertEqual(milestone.display, "未核实")
            self.assertTrue(milestone.detail)

    def test_table_order_is_fixed(self) -> None:
        table = milestone_table(extract_milestones([order(0, at(0))]))
        self.assertEqual(
            [entry["key"] for entry in table],
            [
                "first_order",
                "longest_streak",
                "biggest_order",
                "priciest_night_order",
                "total_spend",
                "most_ordered_item",
            ],
        )

    def test_missing_items_marks_item_milestone_unavailable(self) -> None:
        result = extract_milestones([order(0, at(0), items=[])])
        self.assertFalse(result["most_ordered_item"].available)
        self.assertIn("商品明细", result["most_ordered_item"].detail)

    def test_criteria_documentation_covers_all_six(self) -> None:
        criteria = describe_milestone_criteria()
        self.assertEqual(len(criteria), 6)
        for entry in criteria:
            self.assertTrue(entry["title"])
            self.assertTrue(entry["criteria"])


if __name__ == "__main__":
    unittest.main()
