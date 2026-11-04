"""D37：告警闭环 —— 阈值 → 自动驱逐 → 留痕。

只做**本机能验证**的三类规则：

1. ``QUEUE_BACKLOG``：排队深度超过阈值；
2. ``NODE_NOT_READY``：节点租约失效 / 已 cordon；
3. ``SANDBOX_FAILED``：处于 failed 的沙箱数超过阈值。

「自动驱逐」= 对相关节点执行 cordon + drain（幂等），并写一条
``alert.fired`` 审计。不做重启、不杀进程 —— 那是运维动作，不自动化。
"""

from __future__ import annotations

import os
import sqlite3

from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from src import config
from src.platform.audit import AuditLog
from src.platform.exec_queue import get_queue
from src.platform.node_registry import NodeRegistry

QUEUE_DEPTH_ENV = "PERSONAL_AGENT_ALERT_QUEUE_DEPTH"
FAILED_SANDBOX_ENV = "PERSONAL_AGENT_ALERT_FAILED_SANDBOXES"

DEFAULT_QUEUE_DEPTH = 8
DEFAULT_FAILED_SANDBOXES = 3


@dataclass
class Alert:
    code: str
    message: str
    node_id: str | None = None
    detail: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _threshold(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()

    try:
        value = int(raw)
    except ValueError:
        return default

    return value if value > 0 else default


def _failed_sandboxes() -> int:
    try:
        with closing(sqlite3.connect(str(config.paths.platform_db))) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM agent_sandboxes WHERE state = 'failed'"
            ).fetchone()
            return int(row[0] or 0)
    except sqlite3.Error:
        return 0


def evaluate(now: datetime | None = None) -> list[Alert]:
    alerts: list[Alert] = []

    queue = get_queue().snapshot()
    threshold = _threshold(QUEUE_DEPTH_ENV, DEFAULT_QUEUE_DEPTH)

    if queue["queued"] >= threshold:
        alerts.append(
            Alert(
                code="QUEUE_BACKLOG",
                message=f"排队深度 {queue['queued']} ≥ 阈值 {threshold}",
                detail=queue,
            )
        )

    try:
        nodes = NodeRegistry().list_nodes(now=now)
    except Exception:  # noqa: BLE001 - 节点表没建好时不让健康检查炸
        nodes = []

    for node in nodes:
        if node["removed"]:
            continue

        if node["state"] != "ready" and not node["cordoned"]:
            alerts.append(
                Alert(
                    code="NODE_NOT_READY",
                    message=(
                        f"节点 {node['node_id']} 状态 {node['state']}"
                        f"（generation={node['generation']}）"
                    ),
                    node_id=node["node_id"],
                    detail={"state": node["state"]},
                )
            )

    failed = _failed_sandboxes()
    failed_threshold = _threshold(FAILED_SANDBOX_ENV, DEFAULT_FAILED_SANDBOXES)

    if failed >= failed_threshold:
        alerts.append(
            Alert(
                code="SANDBOX_FAILED",
                message=f"failed 沙箱 {failed} ≥ 阈值 {failed_threshold}",
                detail={"failed": failed},
            )
        )

    return alerts


def apply(alerts: list[Alert]) -> list[dict[str, Any]]:
    """执行自动动作（目前只有节点驱逐），并把每条告警写成审计。"""

    registry = NodeRegistry()
    log = AuditLog()
    applied: list[dict[str, Any]] = []

    for alert in alerts:
        actions: list[str] = []

        if alert.node_id is not None:
            try:
                registry.cordon(alert.node_id)
                registry.drain(alert.node_id)
                actions = ["cordon", "drain"]
            except Exception:  # noqa: BLE001 - 驱逐失败也要留痕
                actions = ["failed"]

        record = log.record(
            "alert.fired",
            outcome="applied" if actions else "observed",
            code=alert.code,
            sandbox_id=None,
            exec_id=None,
            detail={**alert.as_dict(), "actions": actions},
        )

        applied.append(
            {
                "code": alert.code,
                "node_id": alert.node_id,
                "actions": actions,
                "audit_id": record["id"],
            }
        )

    return applied
