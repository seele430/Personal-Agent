"""W3C Trace Context（traceparent）—— 让宿主与沙箱子进程共享同一条 trace。

格式固定为版本 00：

    00-<32 位小写十六进制 trace-id>-<16 位 span-id>-01

约定：

- **trace-id 在一条调用链上不变**：宿主生成一次，沙箱里的子进程通过
  ``TRACEPARENT`` 环境变量拿到同一个 trace-id。
- **span-id 每跳都换**：子进程用 ``child_traceparent()`` 派生自己的 span，
  parent-id 指向宿主那一跳。
- 这里只做注入与校验，不采集、不上报 —— 没有 OpenTelemetry SDK 依赖。
"""

from __future__ import annotations

import hashlib
import re
import secrets

TRACEPARENT_ENV = "TRACEPARENT"

TRACEPARENT_PATTERN = re.compile(
    r"^00-([0-9a-f]{32})-([0-9a-f]{16})-01$"
)

ZERO_TRACE_ID = "0" * 32
ZERO_SPAN_ID = "0" * 16


def _random_trace_id() -> str:
    return secrets.token_hex(16)


def _random_span_id() -> str:
    return secrets.token_hex(8)


def new_traceparent(
    *,
    trace_id: str | None = None,
    span_id: str | None = None,
) -> str:
    """生成一个合法的 traceparent；显式给的 id 只做格式校验。"""

    trace = trace_id or _random_trace_id()
    span = span_id or _random_span_id()

    value = f"00-{trace}-{span}-01"

    if not is_valid(value):
        raise ValueError(f"非法 traceparent：{value}")

    return value


def is_valid(value: str | None) -> bool:
    if not isinstance(value, str):
        return False

    match = TRACEPARENT_PATTERN.fullmatch(value)

    if match is None:
        return False

    trace_id, span_id = match.groups()

    # 全零 id 在规范里表示「无效」，不能当成真的 span。
    return trace_id != ZERO_TRACE_ID and span_id != ZERO_SPAN_ID


def parse(value: str) -> tuple[str, str]:
    """拆出 (trace_id, span_id)；格式不对抛 ValueError。"""

    if not is_valid(value):
        raise ValueError(f"非法 traceparent：{value}")

    match = TRACEPARENT_PATTERN.fullmatch(value)
    assert match is not None  # is_valid 已经保证

    return match.group(1), match.group(2)


def child_traceparent(parent: str, *, span_id: str | None = None) -> str:
    """派生下一跳：复用 trace-id，换新 span-id。"""

    trace_id, _ = parse(parent)

    return new_traceparent(trace_id=trace_id, span_id=span_id)


def traceparent_for_run(run_id: str) -> str:
    """同一个 run 复用同一 trace-id，便于把多次 exec 串成一条链。"""

    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:32]

    return new_traceparent(trace_id=digest)
