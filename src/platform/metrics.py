"""D25'：Prometheus 文本格式指标。

六个核心指标：

1. ``personal_agent_execs_total{status}`` —— 按终态计数的 exec
2. ``personal_agent_exec_duration_seconds`` —— summary（count / sum）
3. ``personal_agent_exec_active`` —— 当前活跃 exec 数
4. ``personal_agent_queue_depth{state}`` —— 队列深度（active / queued）
5. ``personal_agent_sandboxes{state}`` —— 按状态计数的沙箱
6. ``personal_agent_nodes_ready`` —— 租约有效的节点数

口径说明：1、2、5 来自 SQLite（跨进程共享），3、4、6 是**当前进程视角**。
多 worker 时前三个是全局真值，后三个各报各的。
"""

from __future__ import annotations

import math
import sqlite3

from contextlib import closing
from typing import Iterable

from src import config

TEXT_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _query(db_path, sql: str, parameters: Iterable = ()) -> list[tuple]:
    try:
        with closing(sqlite3.connect(str(db_path))) as conn:
            return conn.execute(sql, tuple(parameters)).fetchall()
    except sqlite3.Error:
        return []


def _escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def _fmt(value: float | int) -> str:
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return "0"
        return f"{value:.6f}".rstrip("0").rstrip(".") or "0"
    return str(value)


def collect() -> dict:
    db_path = config.paths.platform_db

    exec_states = dict(
        _query(db_path, "SELECT state, COUNT(*) FROM platform_execs GROUP BY state")
    )
    sandbox_states = dict(
        _query(
            db_path,
            "SELECT state, COUNT(*) FROM agent_sandboxes GROUP BY state",
        )
    )

    duration_rows = _query(
        db_path,
        """
        SELECT COUNT(*), COALESCE(SUM(
            (julianday(finished_at) - julianday(started_at)) * 86400.0
        ), 0)
        FROM platform_execs
        WHERE started_at IS NOT NULL AND finished_at IS NOT NULL
        """,
    )
    duration_count, duration_sum = (
        duration_rows[0] if duration_rows else (0, 0.0)
    )

    from src.platform import active_execs
    from src.platform.exec_queue import get_queue
    from src.platform.node_registry import NodeRegistry

    queue = get_queue().snapshot()

    try:
        nodes_ready = sum(
            1
            for node in NodeRegistry().list_nodes()
            if node["state"] == "ready"
        )
    except Exception:  # noqa: BLE001 - 指标接口不能因为节点表没建好而 500
        nodes_ready = 0

    return {
        "exec_states": exec_states,
        "sandbox_states": sandbox_states,
        "duration_count": int(duration_count or 0),
        "duration_sum": float(duration_sum or 0.0),
        "active_execs": len(active_execs.snapshot()),
        "queue_active": int(queue["active"]),
        "queue_queued": int(queue["queued"]),
        "nodes_ready": int(nodes_ready),
    }


def render() -> str:
    data = collect()
    lines: list[str] = []

    def family(name: str, kind: str, help_text: str) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")

    family(
        "personal_agent_execs_total",
        "counter",
        "Exec 总数，按终态分组",
    )
    for state, count in sorted(data["exec_states"].items()):
        lines.append(
            f'personal_agent_execs_total{{status="{_escape(state)}"}} {int(count)}'
        )

    family(
        "personal_agent_exec_duration_seconds",
        "summary",
        "已结束 exec 的执行时长（秒）",
    )
    lines.append(
        f"personal_agent_exec_duration_seconds_count {data['duration_count']}"
    )
    lines.append(
        f"personal_agent_exec_duration_seconds_sum {_fmt(data['duration_sum'])}"
    )

    family(
        "personal_agent_exec_active",
        "gauge",
        "当前进程内活跃 exec 数",
    )
    lines.append(f"personal_agent_exec_active {data['active_execs']}")

    family(
        "personal_agent_queue_depth",
        "gauge",
        "执行队列深度（当前进程视角）",
    )
    lines.append(
        f'personal_agent_queue_depth{{state="active"}} {data["queue_active"]}'
    )
    lines.append(
        f'personal_agent_queue_depth{{state="queued"}} {data["queue_queued"]}'
    )

    family(
        "personal_agent_sandboxes",
        "gauge",
        "沙箱数量，按状态分组",
    )
    for state, count in sorted(data["sandbox_states"].items()):
        lines.append(
            f'personal_agent_sandboxes{{state="{_escape(state)}"}} {int(count)}'
        )

    family(
        "personal_agent_nodes_ready",
        "gauge",
        "租约有效且 ready 的节点数",
    )
    lines.append(f"personal_agent_nodes_ready {data['nodes_ready']}")

    return "\n".join(lines) + "\n"
