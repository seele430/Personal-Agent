"""单用户助手的 run / turn / step / exec SQLite 数据模型。

旧房间 messages 仅由 run_migration 在数据库副本上迁移；本模块不读取或
修改旧 stage，也不把历史模型发言伪装成工具 exec。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import paths


RUN_MODEL_VERSION = 1
RUN_STATUSES = ("running", "succeeded", "failed", "cancelled", "unknown")
EXEC_STATUSES = (
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def init_run_schema(conn: sqlite3.Connection) -> None:
    """Create additive v1 tables without committing the caller's transaction."""

    # sqlite3.executescript() may implicitly COMMIT a pending transaction.
    # Individual execute() calls keep schema and migration data atomic.
    statements = (
        """
        CREATE TABLE IF NOT EXISTS run_model_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS agent_runs (
            id TEXT PRIMARY KEY,
            request_id TEXT UNIQUE,
            owner_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('running', 'succeeded', 'failed', 'cancelled', 'unknown')
            ),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS agent_turns (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL CHECK (sequence > 0),
            user_message TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (run_id, sequence)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS agent_steps (
            id TEXT PRIMARY KEY,
            turn_id TEXT NOT NULL REFERENCES agent_turns(id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL CHECK (sequence > 0),
            kind TEXT NOT NULL,
            actor TEXT NOT NULL,
            actor_name TEXT,
            content TEXT NOT NULL,
            status TEXT NOT NULL,
            source_message_id INTEGER UNIQUE,
            created_at TEXT NOT NULL,
            UNIQUE (turn_id, sequence)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS agent_execs (
            id TEXT PRIMARY KEY,
            step_id TEXT NOT NULL UNIQUE
                REFERENCES agent_steps(id) ON DELETE CASCADE,
            sandbox_id TEXT,
            tool_name TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN (
                    'queued', 'running', 'succeeded', 'failed',
                    'cancelled', 'timed_out'
                )
            ),
            request_json TEXT NOT NULL,
            result_json TEXT,
            error_code TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_agent_turns_run
            ON agent_turns(run_id, sequence)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_agent_steps_turn
            ON agent_steps(turn_id, sequence)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_agent_execs_status
            ON agent_execs(status)
        """,
    )
    for statement in statements:
        conn.execute(statement)
    conn.execute(
        """
        INSERT INTO run_model_meta(key, value) VALUES ('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(RUN_MODEL_VERSION),),
    )


class RunStore:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or paths.chat_requests_db)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn, conn:
            init_run_schema(conn)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def create_run(
        self,
        *,
        owner_id: str,
        run_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        run_id = run_id or new_id("run")
        now = utc_now()
        with closing(self.connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO agent_runs(
                    id, request_id, owner_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, 'running', ?, ?)
                """,
                (run_id, request_id, owner_id, now, now),
            )
        return self.get_run(run_id, owner_id=owner_id, include_steps=False)

    def create_turn(
        self,
        run_id: str,
        *,
        user_message: str,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        turn_id = turn_id or new_id("turn")
        with closing(self.connect()) as conn, conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM agent_turns WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            sequence = int(row[0])
            conn.execute(
                """
                INSERT INTO agent_turns(
                    id, run_id, sequence, user_message, status, created_at
                ) VALUES (?, ?, ?, ?, 'running', ?)
                """,
                (turn_id, run_id, sequence, user_message, utc_now()),
            )
        return {
            "id": turn_id,
            "run_id": run_id,
            "sequence": sequence,
            "user_message": user_message,
            "status": "running",
        }

    def add_step(
        self,
        turn_id: str,
        *,
        kind: str,
        actor: str,
        content: str,
        step_id: str | None = None,
        actor_name: str | None = None,
        status: str = "running",
    ) -> dict[str, Any]:
        step_id = step_id or new_id("step")
        with closing(self.connect()) as conn, conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM agent_steps WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            sequence = int(row[0])
            conn.execute(
                """
                INSERT INTO agent_steps(
                    id, turn_id, sequence, kind, actor, actor_name,
                    content, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    step_id,
                    turn_id,
                    sequence,
                    kind,
                    actor,
                    actor_name,
                    content,
                    status,
                    utc_now(),
                ),
            )
        return {
            "id": step_id,
            "turn_id": turn_id,
            "sequence": sequence,
            "kind": kind,
            "actor": actor,
            "actor_name": actor_name,
            "content": content,
            "status": status,
            "exec_id": None,
        }

    def update_step(self, step_id: str, *, status: str, content: str) -> None:
        with closing(self.connect()) as conn, conn:
            conn.execute(
                "UPDATE agent_steps SET status = ?, content = ? WHERE id = ?",
                (status, content, step_id),
            )

    def record_exec(
        self,
        step_id: str,
        *,
        tool_name: str,
        request: dict[str, Any],
        exec_id: str | None = None,
        sandbox_id: str | None = None,
        status: str = "queued",
    ) -> dict[str, Any]:
        if status not in EXEC_STATUSES:
            raise ValueError("exec 状态无效")
        exec_id = exec_id or new_id("exec")
        now = utc_now()
        started_at = now if status == "running" else None
        with closing(self.connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO agent_execs(
                    id, step_id, sandbox_id, tool_name, status,
                    request_json, created_at, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    exec_id,
                    step_id,
                    sandbox_id,
                    tool_name,
                    status,
                    json.dumps(request, ensure_ascii=False, sort_keys=True),
                    now,
                    started_at,
                ),
            )
        return self.get_exec(exec_id)

    def update_exec(
        self,
        exec_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> None:
        if status not in EXEC_STATUSES:
            raise ValueError("exec 状态无效")
        now = utc_now()
        with closing(self.connect()) as conn, conn:
            conn.execute(
                """
                UPDATE agent_execs
                SET status = ?, result_json = ?, error_code = ?,
                    started_at = COALESCE(started_at, ?), finished_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    None if result is None else json.dumps(result, ensure_ascii=False),
                    error_code,
                    now,
                    now,
                    exec_id,
                ),
            )

    def finish(self, run_id: str, *, status: str) -> None:
        if status not in RUN_STATUSES:
            raise ValueError("run 状态无效")
        with closing(self.connect()) as conn, conn:
            conn.execute(
                "UPDATE agent_runs SET status = ?, updated_at = ? WHERE id = ?",
                (status, utc_now(), run_id),
            )
            conn.execute(
                """
                UPDATE agent_turns SET status = ?
                WHERE run_id = ? AND status = 'running'
                """,
                (status, run_id),
            )

    def get_exec(self, exec_id: str) -> dict[str, Any]:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM agent_execs WHERE id = ?", (exec_id,)
            ).fetchone()
            if row is None:
                raise KeyError(exec_id)
            return _exec_dict(row)

    def get_run(
        self,
        run_id: str,
        *,
        owner_id: str | None = None,
        include_steps: bool = True,
    ) -> dict[str, Any]:
        with closing(self.connect()) as conn:
            if owner_id is None:
                row = conn.execute(
                    "SELECT * FROM agent_runs WHERE id = ?", (run_id,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM agent_runs WHERE id = ? AND owner_id = ?",
                    (run_id, owner_id),
                ).fetchone()
            if row is None:
                raise KeyError(run_id)
            result = dict(row)
            result["turns"] = []
            if not include_steps:
                return result

            turns = conn.execute(
                "SELECT * FROM agent_turns WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall()
            for turn in turns:
                turn_dict = dict(turn)
                turn_dict["steps"] = []
                steps = conn.execute(
                    """
                    SELECT s.*, e.id AS exec_id
                    FROM agent_steps s
                    LEFT JOIN agent_execs e ON e.step_id = s.id
                    WHERE s.turn_id = ? ORDER BY s.sequence
                    """,
                    (turn["id"],),
                ).fetchall()
                for step in steps:
                    step_dict = dict(step)
                    exec_id = step_dict.pop("exec_id")
                    step_dict["exec_id"] = exec_id
                    step_dict["exec"] = (
                        self.get_exec(exec_id) if exec_id is not None else None
                    )
                    turn_dict["steps"].append(step_dict)
                result["turns"].append(turn_dict)
            return result


def _exec_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["request"] = json.loads(result.pop("request_json"))
    raw_result = result.pop("result_json")
    result["result"] = json.loads(raw_result) if raw_result else None
    return result
