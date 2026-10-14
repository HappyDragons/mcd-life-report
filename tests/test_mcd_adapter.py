"""``mcd_adapter`` 单元测试：字段映射、金额单位换算、退款过滤、窗口与容错。

覆盖点（对应任务书要求）：

- 字段映射：多候选键命中、缺失字段被显式记录；
- 金额单位换算：分 / 元 两种口径、字符串金额、非法输入；
- 退款过滤：整单退款不计入消费额、部分退款按净额计入、状态文本兜底判据；
- 结构容错：多种外层信封、去重、排序、无法识别的结构必须报错而不是静默返回空。
"""

from __future__ import annotations

import json
import os
import re
import unittest
from datetime import datetime, timedelta

from src.mcd_adapter import (
    MCDAdapterError,
    ORDER_LIST_WINDOW,
    Order,
    adapt_order,
    adapt_orders,
    format_yuan,
    parse_timestamp,
    pick,
    to_cents,
    unwrap_payload,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "fixtures", "orders.json")


def raw_order(order_id: str = "SYN-T1", when: str = "2026-05-04 12:20:11", amount: int = 3950) -> dict:
    """构造一条结构完整的原始订单字典。"""
    return {
        "orderId": order_id,
        "orderTime": when,
        "payAmount": amount,
        "refundAmount": 0,
        "orderStatus": "已完成",
        "storeName": "示例门店T（虚构）",
        "products": [{"productName": "巨无霸", "quantity": 1, "price": 2500, "category": "汉堡"}],
    }


class MoneyUnitTests(unittest.TestCase):
    """金额单位换算。"""

    def test_cent_passthrough(self) -> None:
        self.assertEqual(to_cents(3950), 3950)
        self.assertEqual(to_cents(0), 0)
        self.assertEqual(to_cents("3950"), 3950)

    def test_yuan_to_cent(self) -> None:
        self.assertEqual(to_cents(39.5, "yuan"), 3950)
        self.assertEqual(to_cents("39.50", "yuan"), 3950)
        self.assertEqual(to_cents("¥39.50", "yuan"), 3950)
        self.assertEqual(to_cents("1,234.5", "yuan"), 123450)

    def test_rounding_is_half_up_to_cent(self) -> None:
        self.assertEqual(to_cents(12.345, "yuan"), 1235)
        self.assertEqual(to_cents(12.344, "yuan"), 1234)

    def test_invalid_unit_raises(self) -> None:
        with self.assertRaises(MCDAdapterError):
            to_cents(1, "dollar")

    def test_unparsable_returns_none(self) -> None:
        self.assertIsNone(to_cents("abc"))
        self.assertIsNone(to_cents(None))
        self.assertIsNone(to_cents(True))

    def test_format_yuan(self) -> None:
        self.assertEqual(format_yuan(34450), "¥344.50")
        self.assertEqual(format_yuan(0), "¥0.00")
        self.assertEqual(format_yuan(None), "未核实")


class TimestampTests(unittest.TestCase):
    """时间解析与固定时区口径。"""

    def test_plain_datetime(self) -> None:
        order = adapt_order(raw_order(when="2026-05-04 12:20:11"))
        self.assertEqual(order.created_at, datetime(2026, 5, 4, 12, 20, 11))

    def test_iso_with_offset_is_normalised_to_cst(self) -> None:
        # 00:20:11+00:00 等价于北京时间 08:20:11
        order = adapt_order(raw_order(when="2026-05-04T00:20:11+00:00"))
        self.assertEqual(order.created_at, datetime(2026, 5, 4, 8, 20, 11))

    def test_utc_z_suffix(self) -> None:
        order = adapt_order(raw_order(when="2026-05-04T00:20:11Z"))
        self.assertEqual(order.created_at, datetime(2026, 5, 4, 8, 20, 11))

    def test_date_only(self) -> None:
        order = adapt_order(raw_order(when="2026-05-04"))
        self.assertEqual(order.created_at, datetime(2026, 5, 4, 0, 0, 0))

    def test_epoch_seconds_and_milliseconds(self) -> None:
        base = int(datetime(2026, 5, 4, 12, 20, 11).timestamp())
        # 用固定时区换算，避免依赖本机时区：只断言解析成功且落在 2026 年
        self.assertEqual(parse_timestamp(base).strftime("%Y"), "2026")
        self.assertEqual(parse_timestamp(base * 1000).strftime("%Y"), "2026")

    def test_unparsable_is_none_and_recorded(self) -> None:
        order = adapt_order(raw_order(when="不是时间"))
        self.assertIsNone(order.created_at)
        self.assertIn("created_at", order.missing_fields)


class FieldMappingTests(unittest.TestCase):
    """字段映射与缺失记录。"""

    def test_aliases(self) -> None:
        raw = {
            "order_no": "SYN-A1",
            "createTime": "2026-05-04 12:20:11",
            "actualAmount": 1000,
            "shopName": "示例门店Z（虚构）",
            "items": [{"name": "薯条(中)", "num": 2, "unitPrice": 500, "type": "小食"}],
        }
        order = adapt_order(raw)
        self.assertEqual(order.order_id, "SYN-A1")
        self.assertEqual(order.store_name, "示例门店Z（虚构）")
        self.assertEqual(order.items[0].name, "薯条(中)")
        self.assertEqual(order.items[0].quantity, 2)
        self.assertEqual(order.items[0].category, "小食")
        self.assertEqual(order.items[0].line_total_cents, 1000)

    def test_missing_fields_are_recorded_not_guessed(self) -> None:
        order = adapt_order({})
        for field in ("order_id", "created_at", "amount_cents", "store_name", "items"):
            self.assertIn(field, order.missing_fields)
        self.assertEqual(order.order_id, "UNKNOWN-ORDER")
        self.assertEqual(order.amount_cents, 0)
        self.assertEqual(order.items, [])

    def test_quantity_defaults_to_one(self) -> None:
        raw = raw_order()
        raw["products"] = [{"productName": "巨无霸"}]
        order = adapt_order(raw)
        self.assertEqual(order.items[0].quantity, 1)
        self.assertIsNone(order.items[0].unit_price_cents)
        self.assertIsNone(order.items[0].line_total_cents)

    def test_pick_prefers_first_candidate_and_keeps_zero(self) -> None:
        self.assertEqual(pick({"a": None, "b": 0}, ("a", "b")), 0)
        self.assertIsNone(pick({"a": "", "b": None}, ("a", "b")))

    def test_yuan_unit_end_to_end(self) -> None:
        raw = raw_order(amount=39.5)
        order = adapt_order(raw, amount_unit="yuan")
        self.assertEqual(order.amount_cents, 3950)


class RefundFilterTests(unittest.TestCase):
    """退款过滤：整单退款不得计入消费额。"""

    def test_full_refund_is_excluded(self) -> None:
        raw = raw_order(amount=2900)
        raw["refundAmount"] = 2900
        order = adapt_order(raw)
        self.assertTrue(order.is_fully_refunded)
        self.assertEqual(order.net_cents, 0)
        self.assertFalse(order.counted_in_spend)

    def test_partial_refund_counts_net(self) -> None:
        raw = raw_order(amount=4000)
        raw["refundAmount"] = 1000
        order = adapt_order(raw)
        self.assertFalse(order.is_fully_refunded)
        self.assertTrue(order.is_partially_refunded)
        self.assertEqual(order.net_cents, 3000)
        self.assertTrue(order.counted_in_spend)

    def test_over_refund_is_clamped_to_amount(self) -> None:
        raw = raw_order(amount=4000)
        raw["refundAmount"] = 999999
        order = adapt_order(raw)
        self.assertEqual(order.refund_cents, 4000)
        self.assertEqual(order.net_cents, 0)

    def test_status_keyword_fallback(self) -> None:
        raw = raw_order(amount=2900)
        raw.pop("refundAmount")
        raw["orderStatus"] = "已退款"
        order = adapt_order(raw)
        self.assertTrue(order.is_fully_refunded)
        self.assertEqual(order.refund_source, "status")

    def test_explicit_zero_amount_wins_over_status(self) -> None:
        raw = raw_order(amount=2900)
        raw["refundAmount"] = 0
        raw["orderStatus"] = "已取消"
        order = adapt_order(raw)
        self.assertFalse(order.is_fully_refunded)
        self.assertEqual(order.refund_source, "amount")

    def test_refunded_flag_boolean(self) -> None:
        raw = raw_order(amount=2900)
        raw.pop("refundAmount")
        raw["isRefunded"] = True
        order = adapt_order(raw)
        self.assertTrue(order.is_fully_refunded)


class PayloadShapeTests(unittest.TestCase):
    """外层信封容错、去重、排序与窗口提示。"""

    def test_unwrap_shapes(self) -> None:
        one = raw_order()
        self.assertEqual(len(unwrap_payload([one])), 1)
        self.assertEqual(len(unwrap_payload({"orders": [one]})), 1)
        self.assertEqual(len(unwrap_payload({"data": {"orders": [one]}})), 1)
        self.assertEqual(len(unwrap_payload({"data": [one]})), 1)
        self.assertEqual(len(unwrap_payload({"result": {"data": {"list": [one]}}})), 1)
        self.assertEqual(len(unwrap_payload(one)), 1)

    def test_unwrap_invalid_raises(self) -> None:
        with self.assertRaises(MCDAdapterError):
            unwrap_payload("不是结构")
        with self.assertRaises(MCDAdapterError):
            unwrap_payload({"foo": "bar"})
        with self.assertRaises(MCDAdapterError):
            unwrap_payload(None)

    def test_dedupe_by_order_id(self) -> None:
        payload = {"orders": [raw_order("SYN-D1"), raw_order("SYN-D1"), raw_order("SYN-D2")]}
        orders, meta = adapt_orders(payload)
        self.assertEqual(len(orders), 2)
        self.assertEqual(meta["duplicated"], 1)

    def test_sorted_ascending_and_untimed_last(self) -> None:
        payload = {
            "orders": [
                raw_order("SYN-S2", when="2026-06-01 10:00:00"),
                raw_order("SYN-S0", when="坏时间"),
                raw_order("SYN-S1", when="2026-05-01 10:00:00"),
            ]
        }
        orders, meta = adapt_orders(payload)
        self.assertEqual([o.order_id for o in orders], ["SYN-S1", "SYN-S2", "SYN-S0"])
        self.assertEqual(meta["dropped_no_time"], 1)
        self.assertIsNone(orders[-1].created_at)

    def test_window_flag(self) -> None:
        short_payload = {"orders": [raw_order("SYN-W%d" % i) for i in range(3)]}
        _, meta = adapt_orders(short_payload)
        self.assertFalse(meta["may_be_truncated"])
        self.assertEqual(meta["window"], ORDER_LIST_WINDOW)

        full_payload = {
            "orders": [raw_order("SYN-F%d" % i, when="2026-05-%02d 12:00:00" % (i + 1)) for i in range(ORDER_LIST_WINDOW)]
        }
        _, meta_full = adapt_orders(full_payload)
        self.assertTrue(meta_full["may_be_truncated"])

    def test_missing_field_summary(self) -> None:
        payload = {"orders": [{"orderId": "SYN-M1"}]}
        orders, meta = adapt_orders(payload)
        self.assertEqual(len(orders), 1)
        self.assertEqual(meta["missing_field_summary"].get("created_at"), 1)
        self.assertEqual(meta["missing_field_summary"].get("amount_cents"), 1)


class FixtureComplianceTests(unittest.TestCase):
    """仓储内置 fixtures 必须标注为合成数据，且不得含真实凭证/隐私字段。"""

    def setUp(self) -> None:
        with open(FIXTURE, "r", encoding="utf-8") as handle:
            self.payload = json.load(handle)

    def test_fixture_is_marked_synthetic(self) -> None:
        meta = self.payload.get("_meta", {})
        self.assertTrue(meta.get("synthetic"))
        self.assertIn("合成", str(meta.get("label", "")) + str(meta.get("notice", "")))

    def test_fixture_has_ten_orders_matching_mcp_window(self) -> None:
        self.assertEqual(len(self.payload["data"]["orders"]), ORDER_LIST_WINDOW)

    def test_fixture_contains_no_phone_or_long_digit_markers(self) -> None:
        blob = json.dumps(self.payload, ensure_ascii=False)
        self.assertIsNone(re.search(r"1[3-9]\d{9}", blob), "fixtures 中不应出现疑似手机号")
        self.assertIsNone(re.search(r"\b\d{15,}\b", blob), "fixtures 中不应出现超长数字串")
        for token in ("Bearer ", "token", "secret", "password", "AKID"):
            self.assertNotIn(token, blob)

    def test_fixture_amounts_are_integers_in_cents(self) -> None:
        for raw in self.payload["data"]["orders"]:
            self.assertIsInstance(raw["payAmount"], int)
            self.assertIsInstance(raw["refundAmount"], int)

    def test_fixture_loads_through_adapter(self) -> None:
        orders, meta = adapt_orders(self.payload)
        self.assertEqual(len(orders), 10)
        self.assertEqual(meta["duplicated"], 0)
        self.assertEqual(meta["dropped_no_time"], 0)
        self.assertEqual(meta["fully_refunded"], 1)
        self.assertEqual(meta["missing_field_summary"], {})
        self.assertTrue(all(isinstance(o, Order) for o in orders))
        # 时间严格升序
        stamps = [o.created_at for o in orders]
        self.assertEqual(stamps, sorted(stamps))

    def test_fixture_span_is_reasonable(self) -> None:
        orders, _ = adapt_orders(self.payload)
        span = orders[-1].created_at - orders[0].created_at
        self.assertGreater(span, timedelta(days=100))
        self.assertLess(span, timedelta(days=400))


if __name__ == "__main__":
    unittest.main()
