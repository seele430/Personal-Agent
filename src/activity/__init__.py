"""活动摘要（W4）：把 trace 的 span 聚合成中文摘要。"""

from src.activity.summary import (
    ActivitySummary,
    EMPTY_TEXT,
    format_duration,
    summarize_spans,
    summarize_trace,
)


__all__ = [
    "ActivitySummary",
    "EMPTY_TEXT",
    "format_duration",
    "summarize_spans",
    "summarize_trace",
]
