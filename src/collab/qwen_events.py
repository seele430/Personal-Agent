"""Qwen CLI JSON 事件解析（严格合同）。

实测（0.27.0）：
- stdout 是顶层 JSON 数组；
- 事件顺序 `system -> assistant* -> result`；
- `system(subtype=init)` 里有 `session_id` / `model` / `permission_mode`；
- `result` 里有 `subtype` / `is_error` / `usage` / `duration_ms`；
- **没有** `result.success` 字段，成功判定是 `subtype=success and is_error=false`。
"""

from __future__ import annotations

import json


class EventError(ValueError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def parse_events(stdout: str) -> dict:
    """解析并校验事件数组；返回 init/result/assistants。"""

    text = (stdout or "").strip()

    if not text:
        raise EventError("invalid_output", "stdout 为空")

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise EventError("invalid_output", "stdout 不是合法 JSON") from None

    if not isinstance(data, list) or not data:
        raise EventError("invalid_output", "顶层不是非空数组")

    inits = [event for event in data if event.get("type") == "system" and event.get("subtype") == "init"]
    results = [event for event in data if event.get("type") == "result"]

    if len(inits) == 0:
        raise EventError("invalid_output", "缺少 init 事件")

    if len(inits) > 1:
        raise EventError("duplicate_event", "init 事件重复")

    if len(results) == 0:
        raise EventError("missing_result", "缺少 result 事件")

    if len(results) > 1:
        raise EventError("duplicate_event", "result 事件重复")

    if data[0] is not inits[0]:
        raise EventError("invalid_output", "init 不是首个事件")

    if data[-1] is not results[0]:
        raise EventError("invalid_output", "result 不是最后事件")

    return {
        "init": inits[0],
        "result": results[0],
        "assistants": [event for event in data if event.get("type") == "assistant"],
        "events": data,
    }


def observed_model(parsed: dict) -> str | None:
    value = parsed["init"].get("model")

    return value if isinstance(value, str) else None


def check_model(parsed: dict, requested: str, *, aliases: dict[str, str] | None = None) -> str:
    observed = observed_model(parsed)

    if observed is None:
        raise EventError("model_mismatch", "init 缺少 model 字段")

    expected = (aliases or {}).get(requested, requested)

    if observed != expected:
        raise EventError("model_mismatch", f"observed={observed} requested={requested}")

    return observed


def check_session(parsed: dict, expected: str) -> str:
    init_sid = parsed["init"].get("session_id")
    result_sid = parsed["result"].get("session_id")

    if init_sid is None and result_sid is None:
        raise EventError("session_mismatch", "事件中缺少 session_id")

    for value in (init_sid, result_sid):
        if value is not None and value != expected:
            raise EventError("session_mismatch", f"got={value} expected={expected}")

    return expected


def is_success(parsed: dict) -> bool:
    result = parsed["result"]

    return result.get("subtype") == "success" and result.get("is_error") is False


def usage_of(parsed: dict) -> dict:
    usage = parsed["result"].get("usage") or {}

    def number(key: str):
        value = usage.get(key)

        return int(value) if isinstance(value, (int, float)) else None

    return {
        "input_tokens": number("input_tokens"),
        "output_tokens": number("output_tokens"),
        "total_tokens": number("total_tokens"),
    }
