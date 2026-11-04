"""平台 exec 资源（v1 HTTP 面专用）。

与 run 模型里的 ``agent_execs`` 分开：那个属于助手的 run / step / exec 链，
这个属于平台自己的 API 资源，要带 stdout / stderr / result_code 供 SDK 轮询。
"""

from __future__ import annotations

import json
import sqlite3
import uuid

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src import config

EXEC_QUEUED = "queued"
EXEC_RUNNING = "running"
EXEC_SUCCEEDED = "succeeded"
EXEC_FAILED = "failed"
EXEC_CANCELLED = "cancelled"
EXEC_TIMED_OUT = "timed_out"

EXEC_STATES = (
    EXEC_QUEUED,
    EXEC_RUNNING,
    EXEC_SUCCEEDED,
    EXEC_FAILED,
    EXEC_CANCELLED,
    EXEC_TIMED_OUT,
)

TERMINAL_EXEC_STATES = frozenset(
    {EXEC_SUCCEEDED, EXEC_FAILED, EXEC_CANCELLED, EXEC_TIMED_OUT}
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_exec_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_execs (
            id TEXT PRIMARY KEY,
            sandbox_id TEXT NOT NULL,
            run_id TEXT,
            step_id TEXT,
            state TEXT NOT NULL CHECK (
                state IN (
                    'queued', 'running', 'succeeded',
                    'failed', 'cancelled', 'timed_out'
                )
            ),
            argv_json TEXT NOT NULL,
            timeout_ms INTEGER NOT NULL,
            max_output_bytes INTEGER NOT NULL,
            exit_code INTEGER,
            stdout TEXT,
            stderr TEXT,
            truncated INTEGER NOT NULL DEFAULT 0,
            error_code TEXT,
            result_code TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_platform_execs_sandbox
            ON platform_execs(sandbox_id, created_at)
        """
    )


class ExecStore:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or config.paths.platform_db)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with closing(self.connect()) as conn, conn:
            init_exec_schema(conn)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def create(
        self,
        sandbox_id: str,
        *,
        argv: list[str],
        run_id: str | None,
        step_id: str | None,
        timeout_ms: int,
        max_output_bytes: int,
    ) -> dict[str, Any]:
        exec_id = f"exec_{uuid.uuid4().hex}"

        with closing(self.connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO platform_execs(
                    id, sandbox_id, run_id, step_id, state, argv_json,
                    timeout_ms, max_output_bytes, created_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (
                    exec_id,
                    sandbox_id,
                    run_id,
                    step_id,
                    json.dumps(argv, ensure_ascii=False),
                    timeout_ms,
                    max_output_bytes,
                    utc_now(),
                ),
            )

        return self.get(exec_id)

    def start(self, exec_id: str) -> None:
        with closing(self.connect()) as conn, conn:
            conn.execute(
                """
                UPDATE platform_execs
                SET state = 'running', started_at = COALESCE(started_at, ?)
                WHERE id = ? AND state = 'queued'
                """,
                (utc_now(), exec_id),
            )

    def finish(
        self,
        exec_id: str,
        *,
        state: str,
        exit_code: int | None,
        stdout: str,
        stderr: str,
        truncated: bool,
        error_code: str | None,
        result_code: str,
    ) -> None:
        if state not in TERMINAL_EXEC_STATES:
            raise ValueError(f"非终态：{state}")

        with closing(self.connect()) as conn, conn:
            conn.execute(
                """
                UPDATE platform_execs
                SET state = ?, exit_code = ?, stdout = ?, stderr = ?,
                    truncated = ?, error_code = ?, result_code = ?,
                    started_at = COALESCE(started_at, ?), finished_at = ?
                WHERE id = ?
                """,
                (
                    state,
                    exit_code,
                    stdout,
                    stderr,
                    1 if truncated else 0,
                    error_code,
                    result_code,
                    utc_now(),
                    utc_now(),
                    exec_id,
                ),
            )

    def get(self, exec_id: str) -> dict[str, Any]:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM platform_execs WHERE id = ?", (exec_id,)
            ).fetchone()

        if row is None:
            raise KeyError(exec_id)

        return dict(row)

    def list_by_sandbox(self, sandbox_id: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM platform_execs
                WHERE sandbox_id = ? ORDER BY created_at
                """,
                (sandbox_id,),
            ).fetchall()

        return [dict(row) for row in rows]

    def count_active(self, sandbox_id: str) -> int:
        """该沙箱上还在 queued / running 的 exec 数（D20 状态机用）。"""

        with closing(self.connect()) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM platform_execs
                WHERE sandbox_id = ? AND state IN ('queued', 'running')
                """,
                (sandbox_id,),
            ).fetchone()

        return int(row[0])
