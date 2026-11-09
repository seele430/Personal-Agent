"""QQ 短上下文：只保存最近成功发送的 ask 问答。

约束（v1.4o 计划）：

- 每个 sender 最多最近 2 轮（4 行）；
- 只有发送成功的 Q+A 才写入；
- reset 幂等，只清当前 sender。
"""

from __future__ import annotations

import sqlite3
import time

from contextlib import contextmanager
from pathlib import Path


MAX_TURNS = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_threads (
    sender            INTEGER NOT NULL,
    seq               INTEGER NOT NULL,
    role              TEXT NOT NULL CHECK (role IN ('user','assistant')),
    content           TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    created_at        REAL NOT NULL,
    PRIMARY KEY (sender, seq, role)
);
"""


class ThreadStore:
    def __init__(self, path: str | Path, *, clock=None):
        self.path = str(path)
        self._clock = clock or time.time
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

        with self._session() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def _session(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row

        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def append_turn(self, *, sender: int, question: str, answer: str, message_id: str) -> None:
        now = self._clock()

        with self._session() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM qq_threads WHERE sender=?",
                (int(sender),),
            ).fetchone()
            seq = int(row["max_seq"]) + 1

            connection.execute(
                "INSERT INTO qq_threads (sender, seq, role, content, source_message_id, created_at) "
                "VALUES (?, ?, 'user', ?, ?, ?)",
                (int(sender), seq, question, message_id, now),
            )
            connection.execute(
                "INSERT INTO qq_threads (sender, seq, role, content, source_message_id, created_at) "
                "VALUES (?, ?, 'assistant', ?, ?, ?)",
                (int(sender), seq, answer, message_id, now),
            )
            keep_from = seq - MAX_TURNS + 1

            if keep_from > 1:
                connection.execute(
                    "DELETE FROM qq_threads WHERE sender=? AND seq < ?",
                    (int(sender), keep_from),
                )

    def latest_turn(self, sender: int) -> dict | None:
        with self._session() as connection:
            row = connection.execute(
                "SELECT MAX(seq) AS seq FROM qq_threads WHERE sender=?",
                (int(sender),),
            ).fetchone()

            if row is None or row["seq"] is None:
                return None

            seq = int(row["seq"])
            rows = connection.execute(
                "SELECT role, content, source_message_id FROM qq_threads "
                "WHERE sender=? AND seq=?",
                (int(sender), seq),
            ).fetchall()
            pieces = {row["role"]: dict(row) for row in rows}

            if "user" not in pieces or "assistant" not in pieces:
                return None

            return {
                "question": pieces["user"]["content"],
                "answer": pieces["assistant"]["content"],
                "source_message_id": pieces["user"]["source_message_id"],
                "seq": seq,
            }

    def reset(self, sender: int) -> int:
        with self._session() as connection:
            cursor = connection.execute(
                "DELETE FROM qq_threads WHERE sender=?", (int(sender),)
            )

            return cursor.rowcount

    def count(self, sender: int) -> int:
        with self._session() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM qq_threads WHERE sender=?", (int(sender),)
            ).fetchone()

            return int(row["n"])
