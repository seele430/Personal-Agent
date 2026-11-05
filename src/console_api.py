"""平台控制台 API：一屏聚合路由决策、节点、沙箱、执行与队列。"""

from __future__ import annotations

import json
import sqlite3

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query

from src.auth_api import get_current_user
from src.config import paths
from src.platform.exec_queue import get_queue

router = APIRouter(prefix="/console", tags=["控制台"])


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


@router.get("/overview")
def overview(
    limit: int = Query(default=5, ge=1, le=20),
    user: dict = Depends(get_current_user),
):
    snapshot = get_queue().snapshot()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "viewer": user.get("username"),
        "routes": _read_routes(limit),
        "queue": snapshot,
        **_read_platform(),
    }
