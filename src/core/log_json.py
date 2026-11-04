"""D26'：结构化 JSONL 日志，带 trace_id 关联。

用法：``PERSONAL_AGENT_LOG_JSON=1`` 打开（默认仍是普通文本，避免影响既有
输出与测试）。开启后每一行都是合法 JSON，``trace_id`` 来自当前请求绑定的
trace（``src.core.trace_context``），因此同一请求的日志能按 trace_id 聚合。
"""

from __future__ import annotations

import json
import logging
import os

from datetime import datetime, timezone
from typing import Any

from src.core.trace_context import current_trace_id

JSON_ENV = "PERSONAL_AGENT_LOG_JSON"
LEVEL_ENV = "PERSONAL_AGENT_LOG_LEVEL"

# 允许带进 JSON 的结构化字段（其余 extra 一律忽略，避免把对象塞进日志）。
EXTRA_FIELDS = (
    "event",
    "run_id",
    "exec_id",
    "sandbox_id",
    "status",
    "code",
    "duration_ms",
    "path",
    "method",
)


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        trace_id = current_trace_id()

        if trace_id:
            payload["trace_id"] = trace_id

        for name in EXTRA_FIELDS:
            value = getattr(record, name, None)

            if value is not None:
                payload[name] = value

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


def json_logging_enabled() -> bool:
    return os.getenv(JSON_ENV, "").strip().lower() in {"1", "true", "yes"}


def configure_json_logging() -> bool:
    """把根 logger 换成 JSONL 输出；已经配过就返回 False。"""

    root = logging.getLogger()

    for handler in root.handlers:
        if isinstance(handler.formatter, JsonLineFormatter):
            return False

    handler = logging.StreamHandler()
    handler.setFormatter(JsonLineFormatter())

    root.addHandler(handler)
    root.setLevel(os.getenv(LEVEL_ENV, "INFO").upper())

    return True
