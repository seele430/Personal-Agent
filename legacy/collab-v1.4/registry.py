"""外部任务注册表：按 (provider, external_id) 幂等登记。

为什么幂等是核心：外部 Agent 的事件可能重复投递、webhook 可能重试。
重复登记必须只更新最后状态，不能长出两条任务。

外部 Agent 自报的 "tests_passed" 之类字段**只存档**，不参与本地验收判定
（判定在 verify.py 里，只认本地执行结果）。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile

from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


STATES = ("submitted", "running", "succeeded", "failed", "cancelled", "unknown")


class RegistryError(ValueError):
    """注册表参数不合法。"""


class TaskNotFound(KeyError):
    """任务不存在。"""


@dataclass(frozen=True)
class ExternalTask:
    provider: str
    external_id: str
    run_id: str
    state: str
    artifacts: tuple[str, ...] = ()
    claims: dict = field(default_factory=dict)
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "external_id": self.external_id,
            "run_id": self.run_id,
            "state": self.state,
            "artifacts": list(self.artifacts),
            "claims": dict(self.claims),
            "updated_at": self.updated_at,
        }


class ExternalTaskRegistry:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(
            db_path
            or Path(tempfile.gettempdir()) / "pa-external-tasks.db"
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _init(self) -> None:
        with closing(sqlite3.connect(self.db_path, timeout=10)) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS external_tasks ("
                "provider TEXT NOT NULL, external_id TEXT NOT NULL, "
                "run_id TEXT NOT NULL, state TEXT NOT NULL, "
                "artifacts TEXT NOT NULL DEFAULT '[]', claims TEXT NOT NULL DEFAULT '{}', "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "PRIMARY KEY (provider, external_id))"
            )

    def register(
        self,
        provider: str,
        external_id: str,
        *,
        run_id: str,
        state: str = "submitted",
        artifacts: tuple[str, ...] = (),
        claims: dict | None = None,
    ) -> ExternalTask:
        if not provider.strip() or not external_id.strip():
            raise RegistryError("provider / external_id 不能为空")

        if state not in STATES:
            raise RegistryError(f"未知状态：{state}")

        now = datetime.now(timezone.utc).isoformat()
        payload_artifacts = json.dumps(list(artifacts), ensure_ascii=False)
        payload_claims = json.dumps(claims or {}, ensure_ascii=False)

        with closing(sqlite3.connect(self.db_path, timeout=10)) as connection, connection:
            connection.execute(
                "INSERT INTO external_tasks "
                "(provider, external_id, run_id, state, artifacts, claims, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(provider, external_id) DO UPDATE SET "
                "run_id=excluded.run_id, state=excluded.state, "
                "artifacts=excluded.artifacts, claims=excluded.claims, "
                "updated_at=excluded.updated_at",
                (
                    provider, external_id, run_id, state,
                    payload_artifacts, payload_claims, now, now,
                ),
            )

        return self.get(provider, external_id)

    def get(self, provider: str, external_id: str) -> ExternalTask:
        with closing(sqlite3.connect(self.db_path, timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM external_tasks WHERE provider=? AND external_id=?",
                (provider, external_id),
            ).fetchone()

        if row is None:
            raise TaskNotFound(f"{provider}/{external_id}")

        return self._row(row)

    def update_state(self, provider: str, external_id: str, state: str) -> bool:
        if state not in STATES:
            raise RegistryError(f"未知状态：{state}")

        with closing(sqlite3.connect(self.db_path, timeout=10)) as connection, connection:
            cursor = connection.execute(
                "UPDATE external_tasks SET state=?, updated_at=? "
                "WHERE provider=? AND external_id=?",
                (state, datetime.now(timezone.utc).isoformat(), provider, external_id),
            )

            return cursor.rowcount > 0

    def list(self, *, provider: str | None = None, limit: int = 50) -> list[ExternalTask]:
        if not 1 <= limit <= 500:
            raise RegistryError("limit 必须在 1..500")

        query = "SELECT * FROM external_tasks"
        params: list = []

        if provider:
            query += " WHERE provider=?"
            params.append(provider)

        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        with closing(sqlite3.connect(self.db_path, timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, params).fetchall()

        return [self._row(row) for row in rows]

    @staticmethod
    def _row(row) -> ExternalTask:
        try:
            artifacts = tuple(json.loads(row["artifacts"] or "[]"))

        except json.JSONDecodeError:
            artifacts = ()

        try:
            claims = json.loads(row["claims"] or "{}")

        except json.JSONDecodeError:
            claims = {}

        return ExternalTask(
            provider=str(row["provider"]),
            external_id=str(row["external_id"]),
            run_id=str(row["run_id"]),
            state=str(row["state"]),
            artifacts=artifacts,
            claims=claims if isinstance(claims, dict) else {},
            updated_at=str(row["updated_at"]),
        )
