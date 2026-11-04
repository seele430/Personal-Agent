"""D26：平台审计日志 —— 谁在什么时候对哪个沙箱 / exec 做了什么。

只记结构化事实，不记命令正文（命令可能带敏感参数）；需要追查具体命令时，
带上 `exec_id` 去看 exec 记录即可。

表在 `platform.db` 里，跟沙箱、节点同库，避免再多一个数据库文件。
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

EVENTS = frozenset(
    {
        "sandbox.create",
        "sandbox.destroy",
        "exec.create",
        "exec.cancel",
        "auth.denied",
        "rate_limited",
    }
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_audit_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_audit (
            id TEXT PRIMARY KEY,
            at TEXT NOT NULL,
            event TEXT NOT NULL,
            outcome TEXT NOT NULL,
            code TEXT,
            actor TEXT,
            sandbox_id TEXT,
            exec_id TEXT,
            detail_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_audit_at
            ON agent_audit(at)
        """
    )


class AuditLog:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or config.paths.platform_db)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with closing(self.connect()) as conn, conn:
            init_audit_schema(conn)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def record(
        self,
        event: str,
        *,
        outcome: str,
        code: str | None = None,
        actor: str | None = None,
        sandbox_id: str | None = None,
        exec_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if event not in EVENTS:
            raise ValueError(f"未定义审计事件：{event}")

        record_id = f"aud_{uuid.uuid4().hex[:16]}"

        with closing(self.connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO agent_audit(
                    id, at, event, outcome, code, actor,
                    sandbox_id, exec_id, detail_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    utc_now(),
                    event,
                    outcome,
                    code,
                    actor,
                    sandbox_id,
                    exec_id,
                    (
                        json.dumps(detail, ensure_ascii=False)
                        if detail is not None
                        else None
                    ),
                ),
            )

        return self.get(record_id)

    def get(self, record_id: str) -> dict[str, Any]:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM agent_audit WHERE id = ?", (record_id,)
            ).fetchone()

        if row is None:
            raise KeyError(record_id)

        return _view(row)

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))

        with closing(self.connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_audit
                ORDER BY at DESC, rowid DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [_view(row) for row in rows]


def _view(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    detail = item.pop("detail_json", None)
    item["detail"] = json.loads(detail) if detail else None
    return item
