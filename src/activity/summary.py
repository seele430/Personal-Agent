"""中文活动摘要卡：把一条 trace 的 span 聚合成一句人话。

为什么由代码生成而不是让模型写：摘要是"这次到底做了什么"的事实陈述，
用模型生成就可能出现"看起来像做过"的幻觉。这里只做计数与耗时统计，
**确定、免费、可单测**。

span 来源：`traces.db`（`record_span` 写入）。没有 span 时返回"暂无活动记录"，
不报错 —— 老数据没有轨迹是常态，不该让页面挂掉。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.trace_store import get_trace_store


EMPTY_TEXT = "暂无活动记录"

LLM_PREFIXES = ("llm.",)
TOOL_PREFIXES = ("tool.", "tools.")
COMMAND_PREFIXES = ("exec.", "command.", "sandbox.")
TTS_PREFIXES = ("tts.",)


@dataclass(frozen=True)
class ActivitySummary:
    trace_id: str
    span_count: int
    llm_calls: int
    tool_calls: int
    command_calls: int
    tts_calls: int
    other_calls: int
    error_count: int
    duration_ms: float
    text: str

    @property
    def ok(self) -> bool:
        return self.error_count == 0

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "span_count": self.span_count,
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "command_calls": self.command_calls,
            "tts_calls": self.tts_calls,
            "other_calls": self.other_calls,
            "error_count": self.error_count,
            "duration_ms": round(self.duration_ms, 1),
            "text": self.text,
        }


def format_duration(duration_ms: float) -> str:
    """把毫秒说成人话：毫秒 / 秒 / 分秒。"""

    if not isinstance(duration_ms, (int, float)) or duration_ms < 0:
        raise ValueError("duration_ms 必须是非负数")

    if duration_ms < 1000:
        return f"{duration_ms:.0f} 毫秒"

    seconds = duration_ms / 1000.0

    if seconds < 60:
        return f"{seconds:.1f} 秒"

    minutes = int(seconds // 60)
    rest = seconds - minutes * 60

    return f"{minutes} 分 {rest:.0f} 秒"


def _bucket(name: str) -> str:
    if name.startswith(LLM_PREFIXES):
        return "llm"

    if name.startswith(TOOL_PREFIXES):
        return "tool"

    if name.startswith(COMMAND_PREFIXES):
        return "command"

    if name.startswith(TTS_PREFIXES):
        return "tts"

    return "other"


def summarize_spans(spans: list[dict], *, trace_id: str = "") -> ActivitySummary:
    """把 span 列表聚合成摘要。空列表返回"暂无活动记录"。"""

    if not isinstance(spans, list):
        raise TypeError("spans 必须是列表")

    if not spans:
        return ActivitySummary(
            trace_id=trace_id,
            span_count=0,
            llm_calls=0,
            tool_calls=0,
            command_calls=0,
            tts_calls=0,
            other_calls=0,
            error_count=0,
            duration_ms=0.0,
            text=EMPTY_TEXT,
        )

    counts = {"llm": 0, "tool": 0, "command": 0, "tts": 0, "other": 0}
    errors = 0
    start = None
    end = None

    for span in spans:
        name = span.get("name") or ""
        counts[_bucket(name)] += 1

        if span.get("status") == "error":
            errors += 1

        began = span.get("start_ms")
        lasted = span.get("dur_ms") or 0

        if isinstance(began, (int, float)):
            start = began if start is None else min(start, began)
            finish = began + float(lasted)
            end = finish if end is None else max(end, finish)

    duration = 0.0 if start is None or end is None else max(0.0, end - start)

    parts = []

    if counts["llm"]:
        parts.append(f"{counts['llm']} 次模型调用")

    if counts["tool"]:
        parts.append(f"{counts['tool']} 次工具调用")

    if counts["command"]:
        parts.append(f"{counts['command']} 条命令")

    if counts["tts"]:
        parts.append(f"{counts['tts']} 次语音合成")

    if counts["other"]:
        parts.append(f"{counts['other']} 个其他步骤")

    detail = "、".join(parts) if parts else "没有可描述的步骤"
    ending = "全部成功" if errors == 0 else f"{errors} 个步骤失败"

    return ActivitySummary(
        trace_id=trace_id,
        span_count=len(spans),
        llm_calls=counts["llm"],
        tool_calls=counts["tool"],
        command_calls=counts["command"],
        tts_calls=counts["tts"],
        other_calls=counts["other"],
        error_count=errors,
        duration_ms=duration,
        text=f"共 {len(spans)} 步：{detail}，耗时 {format_duration(duration)}，{ending}",
    )


def summarize_trace(trace_id: str, *, store=None) -> ActivitySummary:
    """从 traces.db 取一条 trace 的 span 并聚合。"""

    if not isinstance(trace_id, str) or not trace_id.strip():
        raise ValueError("trace_id 不能为空")

    store = store or get_trace_store()

    try:
        spans = store.fetch_trace(trace_id.strip())

    except Exception:  # noqa: BLE001 - 轨迹库异常不该让摘要卡整块挂掉
        spans = []

    return summarize_spans(list(spans or []), trace_id=trace_id.strip())
