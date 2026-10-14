"""麦门人生报告（mcd-life-report）核心包。

包含五个职责单一的模块：

- ``mcd_adapter``：麦当劳 MCP ``order-list`` 响应适配层（字段映射、金额单位换算、退款过滤）
- ``chronicle``：订单序列 → 人生分期（初遇期 / 探索期 / 稳定期 / 倦怠期 / 回潮期）
- ``milestones``：里程碑提取（第一次点单、最长连续天数、单笔最大金额、最贵的深夜订单、累计消费、最常点餐品）
- ``narrative``：确定性规则生成「人生旁白」（无随机数、无大模型）
- ``card``：生成 1200×2200 分享长图（Pillow 可选，缺失时降级为自包含 SVG）

本包仅使用 Python 标准库。``card`` 模块会把 Pillow（PIL）当作**可选依赖**处理。
"""

__all__ = ["mcd_adapter", "chronicle", "milestones", "narrative", "card"]
__version__ = "1.0.0"
