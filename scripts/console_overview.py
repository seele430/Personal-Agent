r"""一屏控制台（CLI）：路由决策 / 队列与沙箱 / 节点 / 最近执行。

用法：
    python scripts/console_overview.py --data-dir "$env:TEMP\pa-demo"
    python scripts/console_overview.py --data-dir "$env:TEMP\pa-demo"

说明：只读、离线；输出 JSON（展示型排版已移除，前端控制台负责展示）。
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

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
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

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
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

    # 只保留机器可读输出：展示型排版已移除（前端控制台负责展示）。
    print(json.dumps(overview, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
