"""Sleep 运行记录：独立 SQLite，只读查询用。"""

from __future__ import annotations

import json
import sqlite3

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


MAX_SUMMARY_BYTES = 64 * 1024
MAX_OPS = 200
MAX_EVIDENCE = 20
MAX_KEY = 256
MAX_ERROR = 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS sleep_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    ab_variant TEXT NOT NULL DEFAULT 'none',
    base_snapshot_id INTEGER,
    candidate_id TEXT,
    ops_count INTEGER NOT NULL DEFAULT 0,
    applied_count INTEGER NOT NULL DEFAULT 0,
    execution_status TEXT NOT NULL,
    gate_status TEXT NOT NULL,
    record_status TEXT NOT NULL DEFAULT 'complete',
    recall_before REAL,
    recall_after REAL,
    mrr_before REAL,
    mrr_after REAL,
    summary_json TEXT NOT NULL,
    error_code TEXT NOT NULL DEFAULT '',
    record_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sleep_runs_owner_created
    ON sleep_runs(owner_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_sleep_runs_owner_mode
    ON sleep_runs(owner_id, mode, created_at DESC);
"""

MODES = ("plan", "publish", "ab")
EXECUTION = ("succeeded", "failed", "partial")
GATES = ("not_applicable", "not_run", "passed", "failed")
RECORDS = ("complete", "degraded")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clip(value, limit: int) -> tuple[str, bool]:
    text = "" if value is None else str(value)
    truncated = len(text) > limit

    return text[:limit], truncated


class SleepRunStore:
    def __init__(self, db_path: str | Path | None = None):
        from src import config

        self.db_path = Path(db_path or config.paths.sleep_runs_db)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")

        return connection

    def init_db(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(SCHEMA)

    def add_run(
        self,
        *,
        owner_id: str,
        mode: str,
        ab_variant: str = "none",
        base_snapshot_id: int | None = None,
        candidate_id: str | None = None,
        ops: list[dict] | None = None,
        applied_count: int = 0,
        execution_status: str = "succeeded",
        gate_status: str = "not_applicable",
        metrics: dict | None = None,
        reports: dict | None = None,
        error_code: str = "",
    ) -> int:
        if mode not in MODES:
            raise ValueError("mode 非法")

        if execution_status not in EXECUTION:
            raise ValueError("execution_status 非法")

        if gate_status not in GATES:
            raise ValueError("gate_status 非法")

        ops = ops or []
        degraded = False
        safe_ops = []

        for op in ops[:MAX_OPS]:
            evidence = list(op.get("evidence_ids") or [])[:MAX_EVIDENCE]
            key, cut = _clip(op.get("item_key", ""), MAX_KEY)
            degraded = degraded or cut
            safe_ops.append({
                "op": op.get("op"),
                "reason": op.get("reason"),
                "item_id": op.get("item_id"),
                "item_key": key,
                "evidence_ids": evidence,
                "evidence_keys": [
                    _clip(item, MAX_KEY)[0] for item in (op.get("evidence_keys") or [])[:MAX_EVIDENCE]
                ],
            })

        if len(ops) > MAX_OPS:
            degraded = True

        summary = {
            "ops": safe_ops,
            "reports": reports or {},
            "metrics": metrics or {},
        }
        payload = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))

        if len(payload.encode("utf-8")) > MAX_SUMMARY_BYTES:
            payload = json.dumps(
                {"ops": safe_ops[:20], "reports": {}, "metrics": metrics or {}},
                ensure_ascii=False, separators=(",", ":"),
            )
            degraded = True

        error_code, cut_error = _clip(error_code, MAX_ERROR)
        degraded = degraded or cut_error
        record_status = "degraded" if degraded else "complete"
        metric = metrics or {}

        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "INSERT INTO sleep_runs ("
                " created_at, owner_id, mode, ab_variant, base_snapshot_id, candidate_id,"
                " ops_count, applied_count, execution_status, gate_status, record_status,"
                " recall_before, recall_after, mrr_before, mrr_after,"
                " summary_json, error_code, record_error"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _now(), owner_id, mode, ab_variant, base_snapshot_id, candidate_id,
                    len(ops), applied_count, execution_status, gate_status, record_status,
                    metric.get("recall_before"), metric.get("recall_after"),
                    metric.get("mrr_before"), metric.get("mrr_after"),
                    payload, error_code, "summary_truncated" if degraded else "",
                ),
            )

            return int(cursor.lastrowid)

    def list_runs(
        self,
        owner_id: str,
        *,
        limit: int = 20,
        offset: int = 0,
        mode: str | None = None,
        execution_status: str | None = None,
        gate_status: str | None = None,
    ) -> dict:
        if not 1 <= limit <= 100:
            raise ValueError("limit 必须在 1..100")

        if offset < 0:
            raise ValueError("offset 必须 >=0")

        clauses = ["owner_id = ?"]
        params: list = [owner_id]

        for column, value, allowed in (
            ("mode", mode, MODES),
            ("execution_status", execution_status, EXECUTION),
            ("gate_status", gate_status, GATES),
        ):
            if value:
                if value not in allowed:
                    raise ValueError(f"{column} 非法")

                clauses.append(f"{column} = ?")
                params.append(value)

        where = " AND ".join(clauses)

        with closing(self._connect()) as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM sleep_runs WHERE {where}", params
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT id, created_at, mode, ab_variant, ops_count, applied_count,"
                " execution_status, gate_status, record_status,"
                " recall_before, recall_after, mrr_before, mrr_after"
                f" FROM sleep_runs WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()

        return {"items": [dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}

    def get_run(self, owner_id: str, run_id: int) -> dict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM sleep_runs WHERE id = ? AND owner_id = ?",
                (run_id, owner_id),
            ).fetchone()

        if row is None:
            return None

        data = dict(row)
        data.pop("owner_id", None)
        summary = json.loads(data.pop("summary_json"))

        return {
            **data,
            "ops": summary.get("ops", []),
            "reports": summary.get("reports", {}),
            "metrics": summary.get("metrics", {}),
        }
