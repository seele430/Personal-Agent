"""QQ 出站队列：SQLite 持久化 + 状态机 + 原子认领。

状态链：

```text
received → routing → pending_task → running → sending → sent
                  ↘ awaiting_confirm → pending_task
                  ↘ denied / failed / unknown / cancelled
控制命令：received → pending_control → running_control → done
```

关键不变量：

- `pending_task → running` 与 `running → sending` 都是条件 UPDATE，影响行数必须为 1；
- 发送认领额外要求 `COALESCE(cancelled,0)=0`（取消的线性化点）；
- 确认消费绑定 sender + confirm_id + intent + 版本 + 参数哈希 + 状态 + 未过期；
- 旧行新列保持 NULL，不伪造成“新路由”。
"""

from __future__ import annotations

import json
import sqlite3
import time

from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_outbox (
    message_id TEXT PRIMARY KEY,
    sender     INTEGER NOT NULL,
    command    TEXT NOT NULL,
    question   TEXT,
    state      TEXT NOT NULL,
    reply      TEXT,
    attempts   INTEGER NOT NULL DEFAULT 0,
    error      TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qq_outbox_state ON qq_outbox(state, created_at);
"""

NEW_COLUMNS = {
    "queue": "TEXT",
    "raw_text": "TEXT",
    "intent": "TEXT",
    "intent_version": "INTEGER",
    "params_json": "TEXT",
    "normalized_params_hash": "TEXT",
    "decision_source": "TEXT",
    "policy_decision": "TEXT",
    "confirm_id": "TEXT",
    "confirm_expires_at": "REAL",
    "confirm_used": "INTEGER",
    "cancelled": "INTEGER",
    "router_version": "TEXT",
    "route_schema_version": "INTEGER",
}


class QQOutbox:
    def __init__(self, path: str | Path, *, clock=None):
        self.path = str(path)
        self._clock = clock or time.time
        self._init()

    # ---------- 连接 ----------

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row

        return connection

    @contextmanager
    def _session(self):
        connection = self._connect()

        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _init(self) -> None:
        with self._session() as connection:
            connection.executescript(SCHEMA)
            existing = {
                row["name"] for row in connection.execute("PRAGMA table_info(qq_outbox)")
            }

            for name, kind in NEW_COLUMNS.items():
                if name not in existing:
                    connection.execute(f"ALTER TABLE qq_outbox ADD COLUMN {name} {kind}")

            connection.execute(
                "UPDATE qq_outbox SET queue = CASE WHEN command = 'cancel' THEN 'control' ELSE 'task' END "
                "WHERE queue IS NULL"
            )
            connection.execute(
                "UPDATE qq_outbox SET state = CASE WHEN queue = 'control' THEN 'pending_control' "
                "ELSE 'pending_task' END WHERE state = 'received'"
            )

    # ---------- 入站 ----------

    def enqueue(self, *, message_id: str, sender: int, raw_text: str, queue: str) -> bool:
        now = self._clock()

        with self._session() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO qq_outbox
                    (message_id, sender, command, question, state, attempts,
                     created_at, updated_at, queue, raw_text, confirm_used, cancelled)
                VALUES (?, ?, '', '', ?, 0, ?, ?, ?, ?, 0, 0)
                """,
                (
                    str(message_id),
                    int(sender),
                    "pending_task" if queue == "task" else "pending_control",
                    now,
                    now,
                    queue,
                    raw_text,
                ),
            )

            return cursor.rowcount == 1

    # ---------- 路由结果 ----------

    def set_routing(
        self,
        message_id: str,
        *,
        intent: str,
        params: dict,
        params_hash: str,
        decision_source: str,
        state: str,
        policy_decision: str = "",
    ) -> None:
        with self._session() as connection:
            connection.execute(
                """
                UPDATE qq_outbox
                   SET intent = ?, params_json = ?, normalized_params_hash = ?,
                       decision_source = ?, state = ?, policy_decision = ?,
                       intent_version = 1, router_version = '2.0.0-mvp',
                       route_schema_version = 1, updated_at = ?
                 WHERE message_id = ?
                """,
                (
                    intent,
                    json.dumps(params, ensure_ascii=False),
                    params_hash,
                    decision_source,
                    state,
                    policy_decision,
                    self._clock(),
                    str(message_id),
                ),
            )

    def set_awaiting_confirm(
        self,
        message_id: str,
        *,
        confirm_id: str,
        expires_at: float,
        policy_decision: str = "confirm",
    ) -> None:
        with self._session() as connection:
            connection.execute(
                """
                UPDATE qq_outbox
                   SET state = 'awaiting_confirm', confirm_id = ?,
                       confirm_expires_at = ?, confirm_used = 0,
                       policy_decision = ?, updated_at = ?
                 WHERE message_id = ?
                """,
                (confirm_id, expires_at, policy_decision, self._clock(), str(message_id)),
            )

    # ---------- 认领 ----------

    def claim(self, queue: str) -> dict | None:
        source = "pending_control" if queue == "control" else "pending_task"
        target = "running_control" if queue == "control" else "running"

        with self._session() as connection:
            row = connection.execute(
                "SELECT * FROM qq_outbox WHERE queue = ? AND state = ? ORDER BY created_at LIMIT 1",
                (queue, source),
            ).fetchone()

            if row is None:
                return None

            cursor = connection.execute(
                "UPDATE qq_outbox SET state = ?, attempts = attempts + 1, updated_at = ? "
                "WHERE message_id = ? AND state = ?",
                (target, self._clock(), row["message_id"], source),
            )

            if cursor.rowcount != 1:
                return None

            return dict(row) | {"state": target}

    def claim_send(self, message_id: str) -> bool:
        with self._session() as connection:
            cursor = connection.execute(
                "UPDATE qq_outbox SET state = 'sending', updated_at = ? "
                "WHERE message_id = ? AND state IN ('running', 'running_control') "
                "AND COALESCE(cancelled, 0) = 0",
                (self._clock(), str(message_id)),
            )

            return cursor.rowcount == 1

    # ---------- 确认 ----------

    def consume_confirm(self, *, sender: int, confirm_id: str) -> dict | None:
        now = self._clock()

        with self._session() as connection:
            row = connection.execute(
                """
                SELECT * FROM qq_outbox
                 WHERE sender = ? AND confirm_id = ? AND state = 'awaiting_confirm'
                   AND COALESCE(confirm_used, 0) = 0 AND confirm_expires_at > ?
                 LIMIT 1
                """,
                (int(sender), confirm_id, now),
            ).fetchone()

            if row is None:
                return None

            cursor = connection.execute(
                """
                UPDATE qq_outbox
                   SET state = 'pending_task', confirm_used = 1, updated_at = ?
                 WHERE message_id = ? AND sender = ? AND confirm_id = ?
                   AND intent = ? AND intent_version = 1
                   AND normalized_params_hash = ?
                   AND state = 'awaiting_confirm'
                   AND COALESCE(confirm_used, 0) = 0
                   AND confirm_expires_at > ?
                """,
                (
                    now,
                    row["message_id"],
                    int(sender),
                    confirm_id,
                    row["intent"],
                    row["normalized_params_hash"],
                    now,
                ),
            )

            if cursor.rowcount != 1:
                return None

            return dict(row) | {"state": "pending_task"}

    # ---------- 取消 ----------

    def cancel(self, *, sender: int, target: str = "") -> tuple[str, dict | None]:
        with self._session() as connection:
            if target:
                row = connection.execute(
                    "SELECT * FROM qq_outbox WHERE message_id = ? OR confirm_id = ?",
                    (target, target),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM qq_outbox WHERE sender = ? "
                    "AND state IN ('pending_task','pending_control','awaiting_confirm') "
                    "ORDER BY created_at DESC LIMIT 1",
                    (int(sender),),
                ).fetchone()

            if row is None or int(row["sender"]) != int(sender):
                return "not_found", None

            state = row["state"]

            if state in ("pending_task", "pending_control", "awaiting_confirm"):
                connection.execute(
                    "UPDATE qq_outbox SET state = 'cancelled', cancelled = 1, updated_at = ? "
                    "WHERE message_id = ?",
                    (self._clock(), row["message_id"]),
                )

                return "cancelled", dict(row)

            if state in ("running", "routing"):
                connection.execute(
                    "UPDATE qq_outbox SET cancelled = 1, updated_at = ? WHERE message_id = ?",
                    (self._clock(), row["message_id"]),
                )

                return "running", dict(row)

            if state == "sending":
                return "sending", dict(row)

            return "not_found", dict(row)

    # ---------- 状态 ----------

    def mark_sent(self, message_id: str, *, reply: str) -> None:
        with self._session() as connection:
            connection.execute(
                "UPDATE qq_outbox SET state = 'sent', reply = ?, error = NULL, updated_at = ? "
                "WHERE message_id = ?",
                (reply[:2000], self._clock(), str(message_id)),
            )

    def mark_failed(self, message_id: str, *, error: str) -> None:
        with self._session() as connection:
            connection.execute(
                "UPDATE qq_outbox SET state = 'failed', error = ?, updated_at = ? WHERE message_id = ?",
                (str(error)[:500], self._clock(), str(message_id)),
            )

    def mark_unknown(self, message_id: str, *, error: str) -> None:
        with self._session() as connection:
            connection.execute(
                "UPDATE qq_outbox SET state = 'unknown', error = ?, updated_at = ? WHERE message_id = ?",
                (str(error)[:500], self._clock(), str(message_id)),
            )

    def recover_inflight(self) -> int:
        with self._session() as connection:
            cursor = connection.execute(
                "UPDATE qq_outbox SET state = 'unknown', error = 'process restart', updated_at = ? "
                "WHERE state IN ('running', 'running_control', 'sending')",
                (self._clock(),),
            )

            return cursor.rowcount

    # ---------- 查询 ----------

    def get(self, message_id: str) -> dict | None:
        with self._session() as connection:
            row = connection.execute(
                "SELECT * FROM qq_outbox WHERE message_id = ?", (str(message_id),)
            ).fetchone()

            return dict(row) if row else None

    def counts(self) -> dict[str, int]:
        with self._session() as connection:
            rows = connection.execute(
                "SELECT state, COUNT(*) AS n FROM qq_outbox GROUP BY state"
            ).fetchall()

            return {row["state"]: int(row["n"]) for row in rows}
