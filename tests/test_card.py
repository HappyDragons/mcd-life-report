"""``card`` 单元测试：降级路径、自包含 SVG、几何正确性、内容一致性。

本文件里最重要的是「没有 Pillow 时会发生什么」：
必须**降级成功 + 如实告知**，既不能静默失败，也不能假装画出了 PNG。
测试通过改写 ``card._PIL_IMPORTED`` 来模拟缺依赖环境，不需要真的卸载 Pillow。
"""

from __future__ import annotations

import json
import os
import re
import struct
import tempfile
import unittest
from unittest import mock

from src import card as card_mod
from src.card import (
    BUBBLE_MAX_RADIUS,
    BUBBLE_MIN_RADIUS,
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    REFUNDED_RADIUS,
    CardError,
    build_scene,
    char_units,
    compute_layout,
    find_cjk_font,
    render_card,
    render_svg,
    wrap_text,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "fixtures", "orders.json")


def build_report_fixture() -> dict:
    """用仓库内置 fixtures 构造一份固定时间的报告（保证测试可复现）。"""
    import demo
    from src.mcd_adapter import adapt_orders

    payload, synthetic, notice = demo.load_payload(FIXTURE)
    orders, meta = adapt_orders(payload)
    return demo.build_report(
        orders,
        meta,
        source_label="fixtures/orders.json（合成示例数据，非真实订单）",
        source_notice=notice,
        synthetic=synthetic,
        generated_at="2026-10-14 00:00:00",
    )


class GeometryTests(unittest.TestCase):
    """气泡半径、折行、字符宽度等纯函数。"""

    def test_bubble_radius_is_area_proportional(self) -> None:
        amounts = [1000, 4000, 9000]
        r_small = card_mod._bubble_radius(1000, amounts)
        r_mid = card_mod._bubble_radius(4000, amounts)
        r_big = card_mod._bubble_radius(9000, amounts)

        self.assertAlmostEqual(r_big, BUBBLE_MAX_RADIUS, places=6)
        # 面积 ∝ 金额：中间这一点应落在 (4000-1000)/(9000-1000) = 37.5% 的面积处
        area_ratio = (r_mid ** 2 - r_small ** 2) / (r_big ** 2 - r_small ** 2)
        self.assertAlmostEqual(area_ratio, 0.375, places=6)

    def test_bubble_radius_equal_amounts_are_equal_and_max(self) -> None:
        self.assertAlmostEqual(card_mod._bubble_radius(1000, [1000, 1000]), BUBBLE_MAX_RADIUS, places=6)
        self.assertAlmostEqual(card_mod._bubble_radius(1000, [1000]), BUBBLE_MAX_RADIUS, places=6)

    def test_bubble_radius_has_display_floor(self) -> None:
        # 1 分钱对上 9000 分：按比例算出的半径小于下限，应被抬到 BUBBLE_MIN_RADIUS
        self.assertAlmostEqual(card_mod._bubble_radius(1, [1, 9000]), BUBBLE_MIN_RADIUS, places=6)

    def test_bubble_radius_zero_amount(self) -> None:
        self.assertAlmostEqual(card_mod._bubble_radius(0, [0, 5000]), BUBBLE_MIN_RADIUS, places=6)

    def test_char_units(self) -> None:
        self.assertEqual(char_units("abcd"), 4.0)
        self.assertEqual(char_units("中文"), 4.0)
        self.assertEqual(char_units("中a"), 3.0)

    def test_wrap_text_keeps_within_width(self) -> None:
        text = "这是一段用来测试中文折行的文本，它应该被切成若干行而不是溢出画布边界。" * 3
        lines = wrap_text(text, area_width=400, font_size=20)
        self.assertGreater(len(lines), 1)
        capacity = 400 * 2.0 / 20
        for line in lines:
            self.assertLessEqual(char_units(line), capacity + 0.001, "行超宽：%s" % line)

    def test_wrap_text_max_lines_adds_ellipsis(self) -> None:
        text = "长文本" * 50
        lines = wrap_text(text, area_width=200, font_size=20, max_lines=2)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[-1].endswith("…"))

    def test_wrap_text_forces_newline(self) -> None:
        lines = wrap_text("第一行\n第二行", area_width=1000, font_size=20)
        self.assertEqual(lines, ["第一行", "第二行"])

    def test_x_for_offset_maps_endpoints(self) -> None:
        self.assertAlmostEqual(card_mod._x_for_offset(0.0, 100.0), card_mod.PLOT_X0, places=6)
        self.assertAlmostEqual(card_mod._x_for_offset(100.0, 100.0), card_mod.PLOT_X1, places=6)
        center = card_mod.PLOT_X0 + card_mod.PLOT_WIDTH / 2.0
        self.assertAlmostEqual(card_mod._x_for_offset(None, 100.0), center, places=6)
        self.assertAlmostEqual(card_mod._x_for_offset(10.0, 0.0), center, places=6)

    def test_fit_lines_truncates_with_note(self) -> None:
        lines = ["a"] * 20
        fitted = card_mod._fit_lines(lines, line_height=10, available_height=30, overflow_note="见 README")
        self.assertEqual(len(fitted), 3)
        self.assertEqual(fitted[-1], "见 README")


class FontDiscoveryTests(unittest.TestCase):
    """中文字体发现（决定 PNG 能不能画）。"""

    def test_missing_candidates_returns_none(self) -> None:
        with mock.patch.dict(os.environ, {"MCD_CARD_FONT": ""}):
            self.assertIsNone(find_cjk_font(candidates=("C:/definitely/not/here.ttf",)))

    def test_env_override_has_priority(self) -> None:
        with mock.patch.dict(os.environ, {"MCD_CARD_FONT": __file__}):
            self.assertEqual(find_cjk_font(), __file__)

    def test_returns_declared_path_when_present(self) -> None:
        with mock.patch.dict(os.environ, {"MCD_CARD_FONT": ""}):
            found = find_cjk_font(candidates=(__file__,))
        self.assertEqual(found, __file__)


class SceneTests(unittest.TestCase):
    """场景编译：内容与坐标。"""

    def setUp(self) -> None:
        self.report = build_report_fixture()
        self.scene = build_scene(self.report)

    def test_one_point_per_order(self) -> None:
        self.assertEqual(len(self.scene["timeline"]["points"]), len(self.report["timeline"]))

    def test_refunded_point_uses_hollow_radius(self) -> None:
        refunded = [p for p in self.scene["timeline"]["points"] if p["refunded"]]
        self.assertEqual(len(refunded), 1)
        self.assertAlmostEqual(refunded[0]["r"], REFUNDED_RADIUS, places=6)

    def test_points_inside_plot_area(self) -> None:
        for point in self.scene["timeline"]["points"]:
            self.assertGreaterEqual(point["x"], card_mod.PLOT_X0 - 0.001)
            self.assertLessEqual(point["x"], card_mod.PLOT_X1 + 0.001)

    def test_lane_labels_cover_five_slots(self) -> None:
        self.assertEqual([lane["text"] for lane in self.scene["timeline"]["lane_labels"]], list(card_mod.TIME_SLOTS))

    def test_nearby_points_are_nudged_not_stacked(self) -> None:
        # fixtures 里 07-02 与 07-03 两单都在午市 → 应该被错位而不是完全重叠
        midday = [p for p in self.scene["timeline"]["points"] if p["slot"] == "午市"]
        mid_ys = {round(p["y"], 3) for p in midday}
        self.assertGreater(len(mid_ys), 1)

    def test_ticks_are_limited(self) -> None:
        self.assertLessEqual(len(self.scene["timeline"]["ticks"]), 8)
        self.assertTrue(self.scene["timeline"]["ticks"])

    def test_turn_point_badges_match_turn_points(self) -> None:
        self.assertEqual(len(self.scene["badges"]), len(self.report["turn_points"]))

    def test_phase_bands_have_min_width(self) -> None:
        self.assertTrue(self.scene["phase_bands"])
        for band in self.scene["phase_bands"]:
            self.assertGreaterEqual(band["width"], 3.0)

    def test_milestone_details_are_capped(self) -> None:
        self.assertEqual(len(self.scene["milestones"]), 6)
        for cell in self.scene["milestones"]:
            self.assertLessEqual(len(cell["detail_lines"]), card_mod.MILESTONE_DETAIL_MAX_LINES)

    def test_header_contains_slogan_and_meta(self) -> None:
        self.assertIn("别人问你喜欢吃什么", self.scene["header"]["slogan"])
        self.assertEqual(len(self.scene["header"]["meta_lines"]), 3)
        self.assertTrue(self.scene["header"]["headline_lines"])

    def test_honesty_lines_are_capped_and_explained(self) -> None:
        lines = self.scene["narrative"]["honesty_lines"]
        self.assertLessEqual(len(lines), 3)
        if self.report["narrative"]["honesty"]:
            self.assertTrue(lines[0].startswith("·"))
            if len(self.report["narrative"]["honesty"]) > 1:
                self.assertIn("README", lines[-1])

    def test_layout_blocks_do_not_overlap(self) -> None:
        layout = compute_layout(self.scene)
        ordered_keys = ["header", "timeline_title", "timeline_top", "tick", "turn_title", "turn_body", "band", "legend",
                        "milestone_title", "grid", "narrative_title", "narrative", "verdict_top", "honesty", "footer"]
        values = [layout[key] for key in ordered_keys]
        self.assertEqual(values, sorted(values), "版面版块顺序被破坏，存在重叠风险")
        self.assertLess(layout["footer"], CANVAS_HEIGHT)
        self.assertGreater(layout["narrative"], layout["narrative_title"])


class SvgOutputTests(unittest.TestCase):
    """SVG 输出：自包含 + 内容完整。"""

    def setUp(self) -> None:
        self.report = build_report_fixture()
        self.scene = build_scene(self.report)
        self.svg = render_svg(self.scene)

    def test_canvas_size(self) -> None:
        self.assertIn('width="%d"' % CANVAS_WIDTH, self.svg)
        self.assertIn('height="%d"' % CANVAS_HEIGHT, self.svg)
        self.assertIn('viewBox="0 0 %d %d"' % (CANVAS_WIDTH, CANVAS_HEIGHT), self.svg)

    def test_self_contained_no_external_resources(self) -> None:
        self.assertNotIn("<script", self.svg)
        self.assertNotIn("<image", self.svg)
        self.assertNotIn("xlink:href", self.svg)
        self.assertNotIn("@import", self.svg)
        self.assertNotIn("url(http", self.svg)
        # 唯一的 http 出现位置应当是 SVG 命名空间声明
        urls = re.findall(r"https?://[^\s\"']+", self.svg)
        self.assertEqual(urls, ["http://www.w3.org/2000/svg"])

    def test_contains_key_content(self) -> None:
        self.assertIn("麦门人生报告", self.svg)
        self.assertIn("别人问你喜欢吃什么", self.svg)
        self.assertIn("你的点餐史，就是你的人生编年史", self.svg)
        self.assertIn("时间轴编年史", self.svg)
        for milestone in self.report["milestones"]:
            self.assertIn(milestone["title"], self.svg)
        self.assertIn("非麦当劳官方产品", self.svg)
        self.assertIn("合成示例数据", self.svg)

    def test_contains_all_narrative_lines(self) -> None:
        for line in self.scene["narrative"]["lines"]:
            if line:
                self.assertIn(card_mod._svg_escape(line), self.svg)

    def test_escapes_special_characters(self) -> None:
        report = build_report_fixture()
        report["narrative"]["paragraphs"] = ["含 <script> 与 & 符号的文本"]
        report["narrative"]["verdict"] = "a<b&c"
        scene = build_scene(report)
        svg = render_svg(scene)
        self.assertNotIn("<script>", svg)
        self.assertIn("&lt;script&gt;", svg)


class DegradeTests(unittest.TestCase):
    """降级路径：没有 Pillow / 没有中文字体 / 显式要求 SVG。"""

    def setUp(self) -> None:
        self.report = build_report_fixture()
        self.tmp = tempfile.mkdtemp(prefix="mcd-card-test-")

    def test_no_pillow_degrades_to_svg_with_reason(self) -> None:
        with mock.patch.object(card_mod, "_PIL_IMPORTED", False):
            result = render_card(self.report, self.tmp, basename="nopillow")
        self.assertEqual(result.format, "svg")
        self.assertTrue(result.degraded)
        self.assertIsNotNone(result.reason)
        self.assertIn("Pillow", result.reason or "")
        self.assertIn("1200×2200", result.reason or "")
        self.assertTrue(os.path.isfile(result.path))
        self.assertTrue(result.path.endswith(".svg"))
        self.assertGreater(result.bytes_written, 1000)
        self.assertIn("降级", result.notice())

    def test_no_pillow_never_pretends_to_be_png(self) -> None:
        with mock.patch.object(card_mod, "_PIL_IMPORTED", False):
            result = render_card(self.report, self.tmp, basename="honest")
            self.assertFalse(os.path.exists(os.path.join(self.tmp, "honest.png")))
        self.assertEqual(result.format, "svg")

    def test_force_png_without_pillow_says_png_was_requested(self) -> None:
        with mock.patch.object(card_mod, "_PIL_IMPORTED", False):
            result = render_card(self.report, self.tmp, basename="forcepng", force_backend="png")
        self.assertTrue(result.degraded)
        self.assertIn("要求 PNG", result.reason or "")

    def test_no_cjk_font_degrades_and_says_why(self) -> None:
        with mock.patch.object(card_mod, "_PIL_IMPORTED", True), mock.patch.object(
            card_mod, "find_cjk_font", return_value=None
        ):
            result = render_card(self.report, self.tmp, basename="nofont")
        self.assertEqual(result.format, "svg")
        self.assertTrue(result.degraded)
        self.assertIn("字体", result.reason or "")

    def test_force_svg_reason(self) -> None:
        result = render_card(self.report, self.tmp, basename="forcsvg", force_backend="svg")
        self.assertEqual(result.format, "svg")
        self.assertTrue(result.degraded)
        self.assertIn("显式要求", result.reason or "")

    def test_invalid_backend_raises(self) -> None:
        with self.assertRaises(CardError):
            render_card(self.report, self.tmp, force_backend="pdf")

    def test_creates_output_directory(self) -> None:
        target = os.path.join(self.tmp, "nested", "deeper")
        with mock.patch.object(card_mod, "_PIL_IMPORTED", False):
            result = render_card(self.report, target, basename="nested")
        self.assertTrue(os.path.isdir(target))
        self.assertTrue(os.path.isfile(result.path))

    def test_png_render_raises_clearly_when_unavailable(self) -> None:
        with mock.patch.object(card_mod, "_PIL_IMPORTED", False):
            with self.assertRaises(CardError):
                card_mod.render_png_bytes(build_scene(self.report))


class CapabilityTests(unittest.TestCase):
    """能力播报必须与实际环境一致。"""

    def test_capability_matches_availability(self) -> None:
        capabilities = card_mod.describe_output_capabilities()
        if not card_mod.pillow_available():
            self.assertFalse(capabilities["pillow"])
            self.assertEqual(capabilities["expected_format"], "svg")
            self.assertIn("Pillow", capabilities["degrade_reason"] or "")
        else:
            self.assertTrue(capabilities["pillow"])
            if capabilities["cjk_font"]:
                self.assertEqual(capabilities["expected_format"], "png")
                self.assertIsNone(capabilities["degrade_reason"])
            else:
                self.assertEqual(capabilities["expected_format"], "svg")


class PngOutputTests(unittest.TestCase):
    """PNG 输出（仅在 Pillow + 中文字体都可用时执行，否则跳过而不是假装通过）。"""

    def setUp(self) -> None:
        self.report = build_report_fixture()
        self.tmp = tempfile.mkdtemp(prefix="mcd-card-png-")

    @unittest.skipUnless(card_mod.pillow_available() and find_cjk_font(), "需要 Pillow 与中文字体")
    def test_png_magic_bytes_and_size(self) -> None:
        result = render_card(self.report, self.tmp, basename="png")
        self.assertEqual(result.format, "png")
        self.assertFalse(result.degraded)
        self.assertIsNone(result.reason)
        with open(result.path, "rb") as handle:
            payload = handle.read()
        self.assertEqual(payload[:8], b"\x89PNG\r\n\x1a\n")
        self.assertIn(b"IHDR", payload[:32])
        width, height = struct.unpack(">II", payload[16:24])
        self.assertEqual((width, height), (CANVAS_WIDTH, CANVAS_HEIGHT))

    @unittest.skipUnless(card_mod.pillow_available() and find_cjk_font(), "需要 Pillow 与中文字体")
    def test_png_and_svg_share_the_same_scene(self) -> None:
        result = render_card(self.report, self.tmp, basename="both")
        svg = render_svg(build_scene(self.report))
        self.assertEqual(result.format, "png")
        for line in build_scene(self.report)["narrative"]["lines"]:
            if line:
                self.assertIn(card_mod._svg_escape(line), svg)


class ReportContractTests(unittest.TestCase):
    """报告契约：card 只依赖这些字段，缺字段也不能崩。"""

    def test_missing_optional_fields_still_renders(self) -> None:
        minimal = {
            "generated_at": "2026-10-14 00:00:00",
            "source": {"label": "test", "notice": "测试数据"},
            "window": {"order_count": 0, "counted_orders": 0},
            "totals": {"net_cents": 0},
            "phases": {"flow": "初遇期", "segments": []},
            "milestones": [],
            "timeline": [],
            "narrative": {"headline": "", "paragraphs": [], "verdict": "", "tags": [], "honesty": []},
        }
        scene = build_scene(minimal)
        svg = render_svg(scene)
        self.assertIn("<svg", svg)
        self.assertEqual(scene["milestones"], [])

    def test_empty_timeline_has_no_points(self) -> None:
        scene = build_scene(
            {
                "generated_at": "",
                "source": {},
                "window": {},
                "totals": {},
                "phases": {},
                "milestones": [],
                "timeline": [],
                "narrative": {},
            }
        )
        self.assertEqual(scene["timeline"]["points"], [])
        self.assertEqual(scene["timeline"]["ticks"], [])


if __name__ == "__main__":
    unittest.main()
