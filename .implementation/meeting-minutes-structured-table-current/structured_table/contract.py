"""Domain constants shared by all structured-table adapters."""

from __future__ import annotations

SCHEMA_VERSION = 9
MISSING_VALUE = "待确认"
SOURCE_CODE_NOT_PROVIDED = "原文未提供"
POSITION_CONTEXT_DEFAULT = "信息不足"

DIRECTION_VALUES = {"看多", "看空", "关注", "中性", "信息不足"}
TIME_HORIZON_VALUES = {"短期", "中期", "长期", "未说明"}
MARKET_VALUES = {"A股", "港股", "美股", "其他"}
POSITION_STATE_VALUES = {"持有", "未持有", POSITION_CONTEXT_DEFAULT}
POSITION_PLAN_VALUES = {"无", "计划买入", "计划增持", "计划减持", "计划卖出", "暂不操作"}
CONDITION_TYPE_VALUES = {
    "价格/估值",
    "业绩/基本面",
    "产业供需/价格",
    "产品/技术",
    "政策/事件",
    "市场/流动性",
    "资金/筹码",
    "交易/仓位",
    "未分类",
}
VIEWPOINT_FIELDS = [
    "viewpoint_id",
    "meeting_date",
    "viewpoint_date",
    "target_key",
    "target_name",
    "stock_code",
    "market",
    "presenter",
    "presenter_normalized",
    "direction",
    "time_horizon",
    "position_context",
    "conditions",
    "source_evidence",
]
SUMMARY_FIELDS = [
    ("viewpoint_date", "观点日期"),
    ("target_name", "标的名称"),
    ("stock_code", "股票代码"),
    ("market", "市场"),
    ("presenter", "原始发言人"),
    ("presenter_normalized", "规范发言人"),
    ("direction", "观点方向"),
    ("time_horizon", "观点周期"),
    ("position_context", "持仓信息（辅助）"),
]
SUMMARY_LABELS = {label: field for field, label in SUMMARY_FIELDS}
SUMMARY_LABELS["正式发言人"] = "presenter_normalized"
