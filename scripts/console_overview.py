r"""一屏控制台（CLI）：路由决策 / 队列与沙箱 / 节点 / 最近执行。

用法：
    python scripts/console_overview.py --data-dir "$env:TEMP\pa-demo"
    python scripts/console_overview.py --data-dir "$env:TEMP\pa-demo" --json

说明：这是 W4.1 的 CLI 版（前端页尚未实现）；数据直接读本地 SQLite。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def read_routes(db: Path, limit: int) -> list[dict]:
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
    finally:
        conn.close()

    return [
        {
            "at": row["created_at"],
            **(json.loads(row["payload_json"]) if row["payload_json"] else {}),
        }
        for row in rows
    ]


def read_platform(db: Path) -> dict:
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
                SELECT id, state, exit_code, started_at, finished_at
                FROM platform_execs ORDER BY rowid DESC LIMIT 5
                """
            ).fetchall()
        ]
    finally:
        conn.close()

    return {"nodes": nodes, "sandboxes": sandboxes, "execs": execs}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    data_dir = Path(
        args.data_dir
        or os.getenv("PERSONAL_AGENT_DATA_DIR")
        or str(ROOT)
    )

    overview = {
        "data_dir": str(data_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "routes": read_routes(data_dir / "chat_requests.db", args.limit),
        **read_platform(data_dir / "platform.db"),
    }

    if args.json:
        print(json.dumps(overview, ensure_ascii=False, indent=2))
        return 0

    print(f"== 控制台总览 ==  {data_dir}")
    print(f"生成时间：{overview['generated_at']}")
    print()

    print("-- 最近路由决策 --")

    for route in overview["routes"]:
        intent = route.get("intent") or route.get("decision")
        print(
            f"  {route.get('at', '')}  {intent:18} "
            f"conf={route.get('confidence')} risk={route.get('risk')} "
            f"approval={route.get('requires_approval')}"
        )

    if not overview["routes"]:
        print("  （暂无）")

    print()
    print("-- 节点 --")

    for node in overview["nodes"]:
        print(
            f"  {node['node_id']:12} capacity={node['capacity']} "
            f"state={node['state']} cordoned={bool(node['cordoned_at'])}"
        )

    if not overview["nodes"]:
        print("  （暂无）")

    print()
    print("-- 沙箱 --")
    print("  " + (json.dumps(overview["sandboxes"], ensure_ascii=False) or "{}"))

    print()
    print("-- 最近执行 --")

    for row in overview["execs"]:
        print(
            f"  {row['id']}  {row['state']:10} exit={row['exit_code']} "
            f"{row['started_at']} -> {row['finished_at']}"
        )

    if not overview["execs"]:
        print("  （暂无）")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
