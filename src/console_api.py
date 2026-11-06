"""平台控制台 API：单一快照 + 路由决策聚合 + 执行窗口指标。

``GET /console/snapshot`` 是控制台唯一的轮询入口（V2 §4.1）。每个区块统一
envelope：

    {"data": ... | null, "error": {"code": ..., "message": ...} | null}

单块失败不会让整个控制台空白，但错误必须暴露给前端（V2 §4.1 末段）。
``GET /console/overview`` 保留给既有页面与用例。
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query

from src.auth_api import get_current_user
from src.config import paths
from src.platform.exec_queue import get_queue

logger = logging.getLogger("personal_agent.console")

router = APIRouter(prefix="/console", tags=["控制台"])

# 快照里所有「窗口」类指标共用一个时间窗（V2 §3.1：当前值/趋势/事件窗口一致）。
SERIES_WINDOW_SECONDS = 900
SERIES_WINDOW_LABEL = "15m"

# 样本不足就不出百分位（V2 §5：p50/p99 必须带窗口和样本数）。
PERCENTILE_MIN_SAMPLES = 20
EXEC_WINDOW_LIMIT = 2000


# ----------------------------------------------------------------------
# envelope
# ----------------------------------------------------------------------


def _ok(data):
    return {"data": data, "error": None}


def _fail(code: str, message: str):
    return {"data": None, "error": {"code": code, "message": message}}


def _block(name: str, builder, *args, **kwargs):
    """执行一个区块；失败只影响自己，错误如实上报。"""

    try:
        return _ok(builder(*args, **kwargs))
    except Exception as exc:  # noqa: BLE001 - 单块失败不能拖垮整页
        logger.exception("console snapshot 区块失败: %s", name)
        return _fail("BLOCK_FAILED", f"{name}: {exc}")


# ----------------------------------------------------------------------
# 路由决策（原有能力，迁移到快照里）
# ----------------------------------------------------------------------


def _read_routes(limit: int) -> list[dict]:
    db = paths.chat_requests_db

    if not db.is_file():
        return []

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    try:
        rows = conn.execute(
            """
            SELECT payload_json, created_at
            FROM agent_trajectory_events
            WHERE event_type = 'route_decided'
            ORDER BY created_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    routes = []

    for row in rows:
        payload = {}

        if row["payload_json"]:
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                payload = {}

        routes.append({"at": row["created_at"], **payload})

    return routes


# ----------------------------------------------------------------------
# 平台读模型
# ----------------------------------------------------------------------


def _read_platform() -> dict:
    db = paths.platform_db

    if not db.is_file():
        return {"nodes": [], "sandboxes": {}, "execs": []}

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    try:
        nodes = [
            dict(row)
            for row in conn.execute(
                """
                SELECT node_id, capacity, state, cordoned_at, last_heartbeat
                FROM agent_nodes ORDER BY node_id
                """
            ).fetchall()
        ]

        sandboxes: dict[str, int] = {}

        for row in conn.execute(
            "SELECT state, COUNT(*) AS n FROM agent_sandboxes GROUP BY state"
        ).fetchall():
            sandboxes[row["state"]] = row["n"]

        execs = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, sandbox_id, state, exit_code, started_at, finished_at
                FROM platform_execs ORDER BY rowid DESC LIMIT 5
                """
            ).fetchall()
        ]
    except sqlite3.Error:
        return {"nodes": [], "sandboxes": {}, "execs": []}
    finally:
        conn.close()

    return {"nodes": nodes, "sandboxes": sandboxes, "execs": execs}


def _nodes_block() -> dict:
    from src.platform.elastic_controller import capacity_snapshot
    from src.platform.node_registry import NodeRegistry
    from src.platform.sandbox_service import SandboxService

    registry = NodeRegistry()
    service = SandboxService()
    rows = []

    for node in registry.list_nodes():
        rows.append(
            {
                "node_id": node["node_id"],
                "state": node["state"],
                "capacity": int(node["capacity"]),
                "cordoned": bool(node.get("cordoned")),
                "draining": bool(node.get("draining")),
                "accepting": bool(node.get("accepting")),
                "expired": bool(node.get("expired")),
                "lease_expires_at": node.get("lease_expires_at"),
                "seconds_since_heartbeat": node.get("seconds_since_heartbeat"),
                "active_sandboxes": service.count_active(node["node_id"]),
            }
        )

    return {
        "summary": capacity_snapshot(registry),
        "rows": rows,
    }


def _sandboxes_block(limit: int) -> dict:
    db = paths.platform_db

    if not db.is_file():
        return {"counts": {}, "rows": []}

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    try:
        counts = {
            row["state"]: int(row["n"])
            for row in conn.execute(
                "SELECT state, COUNT(*) AS n FROM agent_sandboxes GROUP BY state"
            ).fetchall()
        }

        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, node_id, tier, state, created_at, expires_at,
                       reclaimed_at, reclaim_reason
                FROM agent_sandboxes
                ORDER BY rowid DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        ]
    finally:
        conn.close()

    return {
        "counts": counts,
        "active_total": sum(
            count
            for state, count in counts.items()
            if state in ("created", "running", "idle")
        ),
        "rows": rows,
    }


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None

    ordered = sorted(values)

    if len(ordered) == 1:
        return ordered[0]

    rank = q * (len(ordered) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)

    if low == high:
        return ordered[low]

    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _percentile_block(values: list[float]) -> dict:
    samples = len(values)
    enough = samples >= PERCENTILE_MIN_SAMPLES

    return {
        "p50": round(_percentile(values, 0.5), 1) if enough else None,
        "p99": round(_percentile(values, 0.99), 1) if enough else None,
        "samples": samples,
        "insufficient_samples": not enough,
    }


def _exec_states(conn) -> dict[str, int]:
    return {
        row["state"]: int(row["n"])
        for row in conn.execute(
            "SELECT state, COUNT(*) AS n FROM platform_execs GROUP BY state"
        ).fetchall()
    }


def _exec_window(conn, window_seconds: int) -> dict:
    """窗口内终态分布 + 成功率 + 队列等待/执行时延百分位（V2 §5 口径）。"""

    rows = conn.execute(
        """
        SELECT created_at, started_at, finished_at, state
        FROM platform_execs
        WHERE finished_at IS NOT NULL
          AND julianday(finished_at) >= julianday('now', ?)
        ORDER BY finished_at DESC
        LIMIT ?
        """,
        (f"-{int(window_seconds)} seconds", EXEC_WINDOW_LIMIT),
    ).fetchall()

    terminal = {"succeeded": 0, "failed": 0, "cancelled": 0, "timed_out": 0}
    waits: list[float] = []
    durations: list[float] = []

    for row in rows:
        state = row["state"]

        if state in terminal:
            terminal[state] += 1

        started = _parse(row["started_at"])
        finished = _parse(row["finished_at"])
        created = _parse(row["created_at"])

        if started is not None and created is not None:
            waits.append((started - created).total_seconds() * 1000.0)

        if started is not None and finished is not None:
            durations.append((finished - started).total_seconds() * 1000.0)

    denominator = terminal["succeeded"] + terminal["failed"] + terminal["timed_out"]
    succeeded = terminal["succeeded"]

    return {
        "window_seconds": window_seconds,
        "samples": len(rows),
        "terminal": terminal,
        "success_rate": {
            "value": round(succeeded / denominator, 4) if denominator else None,
            "numerator": succeeded,
            "denominator": denominator,
            "excluded_cancelled": terminal["cancelled"],
            "note": "取消不计入分母，单独展示",
        },
        "queue_wait_ms": _percentile_block(waits),
        "exec_ms": _percentile_block(durations),
    }


def _parse(value) -> datetime | None:
    if not value:
        return None

    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _executions_block(limit: int) -> dict:
    db = paths.platform_db

    if not db.is_file():
        return {
            "recent": [],
            "states": {},
            "window": _exec_window_empty(SERIES_WINDOW_SECONDS),
        }

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    try:
        recent = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, sandbox_id, state, exit_code, created_at,
                       started_at, finished_at, error_code, traceparent
                FROM platform_execs
                ORDER BY rowid DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        ]
        states = _exec_states(conn)
        window = _exec_window(conn, SERIES_WINDOW_SECONDS)
    finally:
        conn.close()

    return {"recent": recent, "states": states, "window": window}


def _exec_window_empty(window_seconds: int) -> dict:
    return {
        "window_seconds": window_seconds,
        "samples": 0,
        "terminal": {"succeeded": 0, "failed": 0, "cancelled": 0, "timed_out": 0},
        "success_rate": {
            "value": None,
            "numerator": 0,
            "denominator": 0,
            "excluded_cancelled": 0,
            "note": "取消不计入分母，单独展示",
        },
        "queue_wait_ms": _percentile_block([]),
        "exec_ms": _percentile_block([]),
    }


def _elastic_block(queue_snapshot: dict, limit: int) -> dict:
    from dataclasses import asdict

    from src.platform.autoscale import AutoscalePolicy, desired_capacity
    from src.platform.elastic_controller import (
        MODE_COMPUTE_ONLY,
        capacity_snapshot,
        get_controller,
        tick_interval_seconds,
    )
    from src.platform.scale_events import ScaleEventStore

    controller = get_controller()
    policy = controller.policy if controller is not None else AutoscalePolicy()

    running = int(queue_snapshot.get("active", 0))
    queued = int(queue_snapshot.get("queued", 0))

    desired = desired_capacity(running, queued, policy)
    capacity = capacity_snapshot(
        controller.registry if controller is not None else None
    )

    store = ScaleEventStore()
    events = store.recent(limit)

    if controller is None:
        mode = "not_running"
        last = None
        message = "控制器未运行：仅按当前负载计算期望容量，未驱动真实容量"
    else:
        mode = controller.mode
        last = controller.last_decision
        message = (
            "已计算，未驱动真实容量" if mode == MODE_COMPUTE_ONLY else None
        )

    stabilization = {
        "required_samples": int(policy.scale_up_after),
        "satisfied_count": None,
        "satisfied": None,
        "note": "按连续采样次数计，不是秒；未运行控制器时无进度",
    }
    cooldown = {
        "seconds": float(policy.scale_down_cooldown_seconds),
        "active": None,
        "note": "冷却期只阻止缩容",
    }

    if last is not None:
        reason = str(last.get("reason") or "")

        if reason.startswith("scale_up_pending:"):
            try:
                stabilization["satisfied_count"] = int(reason.split(":", 1)[1])
                stabilization["satisfied"] = False
            except ValueError:
                pass
        elif last.get("detail") == "steady":
            stabilization["satisfied"] = True

        if last.get("detail") == "cooldown":
            cooldown["active"] = True

    return {
        "controller_mode": mode,
        "controller_running": controller is not None,
        "tick_interval_seconds": tick_interval_seconds(),
        "desired_capacity": desired,
        "actual_capacity": capacity["nodes_accepting"],
        "capacity_unit": capacity["unit"],
        "capacity": capacity,
        "policy": asdict(policy),
        "inputs": {"running": running, "queued": queued},
        "last_decision": last,
        "stabilization": stabilization,
        "cooldown": cooldown,
        "events": events,
        "event_count": store.count(),
        "message": message,
    }


def _freshness(elastic: dict) -> tuple[bool, str | None]:
    """stale 只由「采样年龄」决定（V2 §4.1 / §5）。"""

    last = elastic.get("last_decision")

    if not last:
        return False, None

    moment = _parse(last.get("at"))

    if moment is None:
        return True, "decision_timestamp_unparsable"

    age = (datetime.now(timezone.utc) - moment).total_seconds()
    limit = max(float(elastic.get("tick_interval_seconds") or 30.0) * 3, 90.0)

    if age > limit:
        return True, f"last_tick_{int(age)}s_ago"

    return False, None


# ----------------------------------------------------------------------
# 路由
# ----------------------------------------------------------------------


@router.get("/snapshot")
def snapshot(
    limit: int = Query(default=10, ge=1, le=50),
    user: dict = Depends(get_current_user),
):
    queue_block = _block("queue", get_queue().snapshot)
    queue_snapshot = queue_block["data"] or {}

    elastic_block = _block("elastic", _elastic_block, queue_snapshot, limit)
    elastic = elastic_block["data"] or {}

    is_stale, stale_reason = _freshness(elastic) if elastic else (False, None)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "series_window": SERIES_WINDOW_LABEL,
        "sample_count": None,
        "data_source": "live",
        "is_stale": is_stale,
        "stale_reason": stale_reason,
        "viewer": user.get("username"),
        "queue": queue_block,
        "nodes": _block("nodes", _nodes_block),
        "sandboxes": _block("sandboxes", _sandboxes_block, limit),
        "executions": _block("executions", _executions_block, limit),
        "elastic": elastic_block,
        "trends": _fail(
            "NOT_COLLECTED",
            "趋势采样未接入：计划为进程内 ring buffer，重启即丢（V2 §4.3）",
        ),
    }


@router.get("/overview")
def overview(
    limit: int = Query(default=5, ge=1, le=20),
    user: dict = Depends(get_current_user),
):
    snapshot_payload = get_queue().snapshot()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "viewer": user.get("username"),
        "routes": _read_routes(limit),
        "queue": snapshot_payload,
        **_read_platform(),
    }
