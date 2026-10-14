"""端到端测试：`demo.py` 全链路（读数据 → 分期 → 里程碑 → 旁白 → 长图）。

这些用例直接调用 ``demo.main()``，因此能覆盖命令行参数、退出码、终端输出与落盘文件，
是「验收标准：python demo.py 必须跑通」的可执行版本。
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import demo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "fixtures", "orders.json")


def run_main(argv) -> tuple:
    """执行 ``demo.main``，返回 ``(退出码, 合并后的标准输出与标准错误)``。"""
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = demo.main(argv)
    return code, out.getvalue() + err.getvalue()


def load_fixture_report() -> dict:
    """直接用 fixtures 构造一份报告（固定生成时间，便于断言）。"""
    payload, synthetic, notice = demo.load_payload(FIXTURE)
    from src.mcd_adapter import adapt_orders

    orders, meta = adapt_orders(payload)
    return demo.build_report(
        orders,
        meta,
        source_label="fixtures/orders.json（合成示例数据，非真实订单）",
        source_notice=notice,
        synthetic=synthetic,
        generated_at="2026-10-14 00:00:00",
    )


class LoadPayloadTests(unittest.TestCase):
    """数据来源标注。"""

    def test_fixture_is_marked_synthetic(self) -> None:
        payload, synthetic, notice = demo.load_payload(FIXTURE)
        self.assertTrue(synthetic)
        self.assertIn("合成", notice)
        self.assertEqual(len(payload["data"]["orders"]), 10)

    def test_missing_input_raises(self) -> None:
        from src.mcd_adapter import MCDAdapterError

        with self.assertRaises(MCDAdapterError):
            demo.load_payload(os.path.join(ROOT, "fixtures", "does-not-exist.json"))

    def test_unlabelled_file_is_not_assumed_synthetic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "orders.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"orders": []}, handle)
            _, synthetic, notice = demo.load_payload(path)
        self.assertFalse(synthetic)
        self.assertIn("未", notice)


class ReportStructureTests(unittest.TestCase):
    """报告结构契约。"""

    def setUp(self) -> None:
        self.report = load_fixture_report()

    def test_report_is_json_serialisable(self) -> None:
        payload = json.dumps(self.report, ensure_ascii=False)
        self.assertGreater(len(payload), 1000)
        restored = json.loads(payload)
        self.assertEqual(restored["window"]["order_count"], 10)

    def test_key_sections_present(self) -> None:
        for key in (
            "generated_at",
            "source",
            "window",
            "totals",
            "phases",
            "milestones",
            "timeline",
            "turn_points",
            "narrative",
            "data_quality",
        ):
            self.assertIn(key, self.report)

    def test_window_numbers_are_consistent(self) -> None:
        window = self.report["window"]
        totals = self.report["totals"]
        self.assertEqual(window["order_count"], 10)
        self.assertEqual(window["counted_orders"], 9)
        self.assertEqual(window["refunded_orders"], 1)
        self.assertTrue(window["may_be_truncated"])
        self.assertEqual(totals["gross_cents"] - totals["refunded_cents"], totals["net_cents"])
        self.assertEqual(totals["net_cents"], 34450)

    def test_phases_cover_all_five_in_fixture(self) -> None:
        counts = self.report["phases"]["counts"]
        for phase in ("初遇期", "探索期", "稳定期", "倦怠期", "回潮期"):
            self.assertIn(phase, counts, "fixtures 应当能演示出 %s" % phase)
        self.assertEqual(sum(counts.values()), 10)

    def test_segments_have_offsets_for_rendering(self) -> None:
        segments = self.report["phases"]["segments"]
        self.assertTrue(segments)
        self.assertIsNotNone(segments[0]["start_offset"])
        self.assertEqual(segments[0]["start_offset"], 0.0)
        for segment in segments:
            self.assertIsNotNone(segment["start_offset"])
            self.assertIsNotNone(segment["end_offset"])
            self.assertGreaterEqual(segment["end_offset"], segment["start_offset"])

    def test_timeline_has_one_entry_per_order(self) -> None:
        self.assertEqual(len(self.report["timeline"]), self.report["window"]["order_count"])
        indices = [entry["index"] for entry in self.report["timeline"]]
        self.assertEqual(indices, sorted(indices))

    def test_turn_points_include_named_ones(self) -> None:
        blob = json.dumps(self.report["turn_points"], ensure_ascii=False)
        self.assertIn("回潮期", blob)
        self.assertIn("倦怠期", blob)
        self.assertIn("最大金额", blob)

    def test_refunded_order_is_flagged_but_present(self) -> None:
        flagged = [entry for entry in self.report["timeline"] if entry["refunded"]]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["net_cents"], 0)
        self.assertGreater(flagged[0]["amount_cents"], 0)

    def test_data_quality_is_populated(self) -> None:
        quality = self.report["data_quality"]
        self.assertTrue(quality["warnings"])
        self.assertTrue(any("最近 10 单" in w for w in quality["warnings"]))
        self.assertEqual(quality["missing_field_summary"], {})


class DeterminismTests(unittest.TestCase):
    """报告的可复现性。"""

    def test_narrative_identical_across_runs(self) -> None:
        first = load_fixture_report()["narrative"]
        second = load_fixture_report()["narrative"]
        self.assertEqual(
            json.dumps(first, ensure_ascii=False, sort_keys=True),
            json.dumps(second, ensure_ascii=False, sort_keys=True),
        )

    def test_full_report_identical_except_generated_at(self) -> None:
        first = load_fixture_report()
        second = load_fixture_report()
        first.pop("generated_at")
        second.pop("generated_at")
        self.assertEqual(
            json.dumps(first, ensure_ascii=False, sort_keys=True),
            json.dumps(second, ensure_ascii=False, sort_keys=True),
        )


class CliTests(unittest.TestCase):
    """命令行行为。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mcd-demo-test-")

    def test_default_run_succeeds_and_writes_card(self) -> None:
        code, output = run_main(["--input", FIXTURE, "--out", self.tmp, "--quiet"])
        self.assertEqual(code, 0)
        self.assertTrue(any(name.startswith("mcd-life-report.") for name in os.listdir(self.tmp)))
        self.assertTrue("mcd-life-report" in output)

    def test_full_report_prints_all_sections(self) -> None:
        code, output = run_main(["--input", FIXTURE, "--out", self.tmp])
        self.assertEqual(code, 0)
        for section in (
            "[数据来源]",
            "[窗口概览]",
            "[人生分期]",
            "[六条里程碑]",
            "[人生转折点]",
            "[人生旁白]",
            "[分享长图]",
            "[诚实性说明与已知限制]",
            "[口径说明]",
        ):
            self.assertIn(section, output, "缺少区块：%s" % section)

    def test_output_admits_synthetic_data(self) -> None:
        _, output = run_main(["--input", FIXTURE, "--out", self.tmp])
        self.assertIn("合成示例数据", output)
        self.assertIn("非真实订单", output)

    def test_output_mentions_degrade_honestly(self) -> None:
        _, output = run_main(["--input", FIXTURE, "--out", self.tmp, "--backend", "svg"])
        self.assertIn("降级", output)
        self.assertIn("显式要求", output)

    def test_json_flag_emits_valid_json(self) -> None:
        code, output = run_main(["--input", FIXTURE, "--out", self.tmp, "--json", "--quiet"])
        self.assertEqual(code, 0)
        start = output.index("{")
        payload = json.loads(output[start:])
        self.assertEqual(payload["window"]["order_count"], 10)

    def test_missing_input_returns_nonzero(self) -> None:
        code, output = run_main(["--input", os.path.join(self.tmp, "nope.json"), "--out", self.tmp])
        self.assertEqual(code, 2)
        self.assertIn("找不到", output)

    def test_broken_json_returns_nonzero(self) -> None:
        path = os.path.join(self.tmp, "broken.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        code, _ = run_main(["--input", path, "--out", self.tmp])
        self.assertEqual(code, 2)

    def test_empty_orders_returns_nonzero(self) -> None:
        path = os.path.join(self.tmp, "empty.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"data": {"orders": []}}, handle)
        code, output = run_main(["--input", path, "--out", self.tmp])
        self.assertEqual(code, 2)
        self.assertIn("空", output)

    def test_backend_png_request_reports_honestly(self) -> None:
        from src import card as card_mod

        code, output = run_main(["--input", FIXTURE, "--out", self.tmp, "--backend", "png", "--quiet"])
        self.assertEqual(code, 0)
        if card_mod.pillow_available() and card_mod.find_cjk_font():
            self.assertIn(".png", output)
        else:
            self.assertIn("降级", output)
            self.assertIn("要求 PNG", output)

    def test_output_path_is_reported(self) -> None:
        _, output = run_main(["--input", FIXTURE, "--out", self.tmp, "--quiet"])
        self.assertIn("mcd-life-report.", output)
        self.assertTrue(".svg" in output or ".png" in output)

    def test_relative_default_input_is_used(self) -> None:
        # 默认 --input 是相对路径 fixtures/orders.json；测试工作目录即仓库根目录
        code, output = run_main(["--out", self.tmp])
        self.assertEqual(code, 0)
        self.assertIn("fixtures", output)


if __name__ == "__main__":
    unittest.main()
