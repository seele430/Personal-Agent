"""QQ 出站队列：SQLite 持久化 + 状态机。

状态：

```text
pending → sending → sent
                  ↘ failed    （明确的网络/HTTP 失败，不重试）
                  ↘ unknown   （超时/进程重启，结果未知，不自动重发）
```

约束：

- `message_id` 是主键，重复入队直接忽略（幂等）；
- 同一任务只会被 claim 一次；
- 进程重启把残留 `sending` 置为 `unknown`，**不自动重发**。
"""

from __future__ import annotations

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


class QQOutbox:
    def __init__(self, path: str | Path, *, clock=None):
        self.path = str(path)
        self._clock = clock or time.time
        self._init()

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

    def enqueue(self, *, message_id: str, sender: int, command: str, question: str = "") -> bool:
        now = self._clock()

        with self._session() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO qq_outbox
                    (message_id, sender, command, question, state, attempts, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (str(message_id), int(sender), command, question, now, now),
            )

            return cursor.rowcount == 1

    def claim_next(self) -> dict | None:
        with self._session() as connection:
            row = connection.execute(
                "SELECT * FROM qq_outbox WHERE state = 'pending' ORDER BY created_at LIMIT 1"
            ).fetchone()

            if row is None:
                return None

            connection.execute(
                "UPDATE qq_outbox SET state = 'sending', attempts = attempts + 1, updated_at = ? WHERE message_id = ?",
                (self._clock(), row["message_id"]),
            )

            return dict(row) | {"state": "sending", "attempts": int(row["attempts"]) + 1}

    def mark_sent(self, message_id: str, *, reply: str, result_message_id: str = "") -> None:
        with self._session() as connection:
            connection.execute(
                "UPDATE qq_outbox SET state = 'sent', reply = ?, error = NULL, updated_at = ? WHERE message_id = ?",
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
                "UPDATE qq_outbox SET state = 'unknown', error = 'process restart', updated_at = ? WHERE state = 'sending'",
                (self._clock(),),
            )

            return cursor.rowcount

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
