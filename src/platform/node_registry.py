"""D17：节点注册与心跳（generation 单调递增）。

契约见 ``docs/platform-contract.md`` 5.1：

- 每次注册都拿到新的、单调不可复用的 ``generation``；
- 心跳必须同时匹配 ``node_id + generation``，旧代际一律 409
  ``NODE_GENERATION_STALE``；
- 租约过期判定（``not_ready``）与 cordon / drain 属于 D18 / D19，这里只记录
  ``last_heartbeat`` 与状态字段，不提前做调度。

当前是单机单节点：创建沙箱时用 ``ensure_default_node()`` 保证 ``node_local``
这一行存在，节点列表因此有真实内容，而不是一张空表。
"""

from __future__ import annotations

import sqlite3
import uuid

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src import config
from src.platform.sandbox_service import PlatformError

DEFAULT_NODE_ID = "node_local"
DEFAULT_CAPACITY = 4
MAX_CAPACITY = 64

DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 10
MAX_HEARTBEAT_INTERVAL_SECONDS = 300
# 契约：停心跳 3 个周期即过期 → not_ready。租约长度 = 间隔 × 该阈值。
MISSED_HEARTBEATS_ALLOWED = 3

NODE_READY = "ready"
NODE_NOT_READY = "not_ready"


class NodeNotFound(PlatformError):
    code = "NODE_NOT_FOUND"
    http_status = 404


class StaleGeneration(PlatformError):
    code = "NODE_GENERATION_STALE"
    http_status = 409


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_node_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_nodes (
            node_id TEXT PRIMARY KEY,
            generation INTEGER NOT NULL,
            capacity INTEGER NOT NULL,
            state TEXT NOT NULL,
            registered_at TEXT NOT NULL,
            last_heartbeat TEXT NOT NULL
        )
        """
    )

    # D18：心跳间隔（租约 = 间隔 × 3）。旧库补列，幂等。
    columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_nodes)")}

    if "heartbeat_interval_seconds" not in columns:
        conn.execute(
            """
            ALTER TABLE agent_nodes
            ADD COLUMN heartbeat_interval_seconds INTEGER NOT NULL DEFAULT 10
            """
        )


class NodeRegistry:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or config.paths.platform_db)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with closing(self.connect()) as conn, conn:
            init_node_schema(conn)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------

    def register(
        self,
        *,
        node_id: str | None = None,
        capacity: int = DEFAULT_CAPACITY,
        heartbeat_interval_seconds: int = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """注册（或重新注册）一个节点；每次注册 generation +1。"""

        node_id = node_id or f"node_{uuid.uuid4().hex[:12]}"

        if not isinstance(node_id, str) or not node_id.strip():
            raise ValueError("node_id 必须是非空字符串")

        if type(capacity) is not int or not 1 <= capacity <= MAX_CAPACITY:
            raise ValueError(f"capacity 必须是 1—{MAX_CAPACITY} 的整数")

        if (
            type(heartbeat_interval_seconds) is not int
            or not 1
            <= heartbeat_interval_seconds
            <= MAX_HEARTBEAT_INTERVAL_SECONDS
        ):
            raise ValueError(
                "heartbeat_interval_seconds 必须是 1—"
                f"{MAX_HEARTBEAT_INTERVAL_SECONDS} 的整数"
            )

        moment = (now or datetime.now(timezone.utc)).isoformat()

        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")

            try:
                row = conn.execute(
                    "SELECT * FROM agent_nodes WHERE node_id = ?", (node_id,)
                ).fetchone()

                if row is None:
                    conn.execute(
                        """
                        INSERT INTO agent_nodes(
                            node_id, generation, capacity, state,
                            registered_at, last_heartbeat,
                            heartbeat_interval_seconds
                        ) VALUES (?, 1, ?, ?, ?, ?, ?)
                        """,
                        (
                            node_id,
                            capacity,
                            NODE_READY,
                            moment,
                            moment,
                            heartbeat_interval_seconds,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE agent_nodes
                        SET generation = generation + 1, capacity = ?,
                            state = ?, registered_at = ?, last_heartbeat = ?,
                            heartbeat_interval_seconds = ?
                        WHERE node_id = ?
                        """,
                        (
                            capacity,
                            NODE_READY,
                            moment,
                            moment,
                            heartbeat_interval_seconds,
                            node_id,
                        ),
                    )

                conn.commit()
            except BaseException:
                conn.rollback()
                raise

        return self.get(node_id, now=now)

    def heartbeat(
        self,
        node_id: str,
        *,
        generation: int,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        moment = (now or datetime.now(timezone.utc)).isoformat()

        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")

            try:
                row = conn.execute(
                    "SELECT * FROM agent_nodes WHERE node_id = ?", (node_id,)
                ).fetchone()

                if row is None:
                    raise NodeNotFound(node_id)

                if int(generation) != int(row["generation"]):
                    # 旧代际的心跳不能复活节点，也不能续新实例的租约。
                    raise StaleGeneration(
                        f"节点 {node_id} 当前 generation="
                        f"{row['generation']}，收到 {generation}"
                    )

                conn.execute(
                    """
                    UPDATE agent_nodes
                    SET last_heartbeat = ?, state = ?
                    WHERE node_id = ?
                    """,
                    (moment, NODE_READY, node_id),
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

        return self.get(node_id, now=now)

    def get(self, node_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM agent_nodes WHERE node_id = ?", (node_id,)
            ).fetchone()

        if row is None:
            raise NodeNotFound(node_id)

        return self._view(row, now)

    def list_nodes(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM agent_nodes ORDER BY node_id"
            ).fetchall()

        return [self._view(row, now) for row in rows]

    @staticmethod
    def _view(row: sqlite3.Row, now: datetime | None) -> dict[str, Any]:
        """状态由 last_heartbeat 推导：超过 3 个心跳周期没说话就是 not_ready。

        这样「过期」不依赖某个后台任务刚好跑过；读到的永远是当前判定。
        """

        interval = int(
            row["heartbeat_interval_seconds"]
            or DEFAULT_HEARTBEAT_INTERVAL_SECONDS
        )
        lease_seconds = interval * MISSED_HEARTBEATS_ALLOWED
        last = datetime.fromisoformat(row["last_heartbeat"])
        deadline = last + timedelta(seconds=lease_seconds)
        moment = now or datetime.now(timezone.utc)

        view = dict(row)
        view["state"] = NODE_READY if moment < deadline else NODE_NOT_READY
        view["lease_seconds"] = lease_seconds
        view["lease_expires_at"] = deadline.isoformat()
        view["seconds_since_heartbeat"] = round(
            max(0.0, (moment - last).total_seconds()), 3
        )
        view["expired"] = moment >= deadline
        return view

    def ensure_default_node(
        self, *, now: datetime | None = None
    ) -> dict[str, Any]:
        """单机部署的默认节点；不存在才注册，存在则原样返回（不提高代际）。"""

        try:
            return self.get(DEFAULT_NODE_ID, now=now)
        except NodeNotFound:
            return self.register(node_id=DEFAULT_NODE_ID, now=now)
