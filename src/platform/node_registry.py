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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src import config
from src.platform.sandbox_service import PlatformError

DEFAULT_NODE_ID = "node_local"
DEFAULT_CAPACITY = 4
MAX_CAPACITY = 64

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
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """注册（或重新注册）一个节点；每次注册 generation +1。"""

        node_id = node_id or f"node_{uuid.uuid4().hex[:12]}"

        if not isinstance(node_id, str) or not node_id.strip():
            raise ValueError("node_id 必须是非空字符串")

        if type(capacity) is not int or not 1 <= capacity <= MAX_CAPACITY:
            raise ValueError(f"capacity 必须是 1—{MAX_CAPACITY} 的整数")

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
                            registered_at, last_heartbeat
                        ) VALUES (?, 1, ?, ?, ?, ?)
                        """,
                        (node_id, capacity, NODE_READY, moment, moment),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE agent_nodes
                        SET generation = generation + 1, capacity = ?,
                            state = ?, registered_at = ?, last_heartbeat = ?
                        WHERE node_id = ?
                        """,
                        (capacity, NODE_READY, moment, moment, node_id),
                    )

                conn.commit()
            except BaseException:
                conn.rollback()
                raise

        return self.get(node_id)

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

        return self.get(node_id)

    def get(self, node_id: str) -> dict[str, Any]:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM agent_nodes WHERE node_id = ?", (node_id,)
            ).fetchone()

        if row is None:
            raise NodeNotFound(node_id)

        return dict(row)

    def list_nodes(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM agent_nodes ORDER BY node_id"
            ).fetchall()

        return [dict(row) for row in rows]

    def ensure_default_node(self) -> dict[str, Any]:
        """单机部署的默认节点；不存在才注册，存在则原样返回（不提高代际）。"""

        try:
            return self.get(DEFAULT_NODE_ID)
        except NodeNotFound:
            return self.register(node_id=DEFAULT_NODE_ID)
