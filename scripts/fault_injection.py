"""D38 故障注入：三种故障各做一次实验并留档。

场景（顺序有讲究：cordon 之后就不能再建沙箱了，所以放最后）：

1. 队列塞满 → 429 `QUEUE_FULL`
2. 节点停心跳 → 租约过期 → `not_ready`
3. 节点被摘除（cordon）→ create 返回 429 `NO_READY_NODE`

产物：``docs/evidence/d38-fault-injection.json``

用法：

    .\\venv\\Scripts\\python.exe scripts\\fault_injection.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EVIDENCE = ROOT / "docs" / "evidence" / "d38-fault-injection.json"

SLEEP = [sys.executable, "-c", "import time; time.sleep(3)"]


def main() -> int:
    import os
    import shutil
    import tempfile

    temp = Path(tempfile.mkdtemp(prefix="personal-agent-fault-"))
    (temp / "data").mkdir(parents=True, exist_ok=True)

    # 环境变量必须在导入 src.main 之前设置：src.config 在导入期就把
    # 数据目录算好了，晚设置等于没设置。
    os.environ.update(
        {
            "PERSONAL_AGENT_SANDBOX_ROOT": str(temp / "sandboxes"),
            "PERSONAL_AGENT_DATA_DIR": str(temp / "data"),
            "PERSONAL_AGENT_PLATFORM_REAPER": "0",
            "PERSONAL_AGENT_PLATFORM_MAX_ACTIVE_EXECS": "1",
            "PERSONAL_AGENT_PLATFORM_QUEUE_SIZE": "1",
            "PERSONAL_AGENT_PLATFORM_RATE_PER_MIN": "0",
            # 故障注入直接打匿名 /v1；R6 之后需要显式声明。
            "PERSONAL_AGENT_ALLOW_ANON_V1": "1",
        }
    )

    try:
        from fastapi.testclient import TestClient

        from src.main import app

        db_path = temp / "data" / "platform.db"

        with TestClient(app) as client:
                created = client.post(
                    "/v1/sandboxes",
                    json={
                        "tier": "tier1",
                        "workspace_id": "ws_fault",
                        "ttl_seconds": 600,
                    },
                    headers={"Idempotency-Key": "fault-001"},
                )
                assert created.status_code == 201, created.text
                sandbox_id = created.json()["sandbox_id"]

                # 1) 队列塞满
                first = client.post(
                    f"/v1/sandboxes/{sandbox_id}/exec",
                    json={"command": SLEEP, "timeout_ms": 30000},
                )
                second = client.post(
                    f"/v1/sandboxes/{sandbox_id}/exec",
                    json={"command": SLEEP, "timeout_ms": 30000},
                )
                third = client.post(
                    f"/v1/sandboxes/{sandbox_id}/exec",
                    json={"command": SLEEP, "timeout_ms": 30000},
                )

                queue_scenario = {
                    "name": "队列塞满",
                    "first": first.json()["state"],
                    "second": second.json()["state"],
                    "third_http": third.status_code,
                    "third_code": third.json()["error"]["code"],
                    "retry_after": third.headers.get("retry-after"),
                }

                for exec_id in (
                    first.json()["exec_id"],
                    second.json()["exec_id"],
                ):
                    client.post(f"/v1/exec/{exec_id}/cancel")

                # 2) 节点停心跳：注册第二个节点，再把它的心跳拨回过去
                registered = client.post(
                    "/v1/nodes",
                    json={"node_id": "node_fault", "capacity": 2},
                )
                node = registered.json()

                db_path = temp / "data" / "platform.db"
                stale = (
                    datetime.now(timezone.utc) - timedelta(minutes=10)
                ).isoformat()

                with closing(sqlite3.connect(db_path)) as conn, conn:
                    conn.execute(
                        "UPDATE agent_nodes SET last_heartbeat = ? "
                        "WHERE node_id = ?",
                        (stale, "node_fault"),
                    )

                nodes = client.get("/v1/nodes").json()["nodes"]
                fault_node = next(
                    item for item in nodes if item["node_id"] == "node_fault"
                )

                heartbeat_scenario = {
                    "name": "节点停心跳",
                    "generation": node["generation"],
                    "state": fault_node["state"],
                    "expired": fault_node["expired"],
                    "seconds_since_heartbeat": fault_node[
                        "seconds_since_heartbeat"
                    ],
                }

                # 3) 节点被摘除
                client.post("/v1/nodes/node_local/cordon")

                blocked = client.post(
                    "/v1/sandboxes",
                    json={
                        "tier": "tier1",
                        "workspace_id": "ws_fault",
                        "ttl_seconds": 600,
                    },
                    headers={"Idempotency-Key": "fault-002"},
                )

                cordon_scenario = {
                    "name": "节点被摘除",
                    "http": blocked.status_code,
                    "code": blocked.json()["error"]["code"],
                }

                report = {
                    "status": "PASS",
                    "scenarios": [
                        queue_scenario,
                        heartbeat_scenario,
                        cordon_scenario,
                    ],
                }

                assert queue_scenario["third_http"] == 429
                assert queue_scenario["third_code"] == "QUEUE_FULL"
                assert heartbeat_scenario["state"] == "not_ready"
                assert heartbeat_scenario["expired"] is True
                assert cordon_scenario["http"] == 429
                assert cordon_scenario["code"] == "NO_READY_NODE"

                EVIDENCE.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0
    finally:
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
