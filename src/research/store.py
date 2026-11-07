"""ResearchGraph 的运行与事件持久化。

两张表，两个用途：

- `research_runs`：一次研究的总账（状态、轮数、重写次数、评分、证据、报告路径）；
- `research_events`：**每次状态迁移**一行（从哪到哪、谁触发、为什么、何时）。

"逐步持久化"落在这里：迁移即写库，所以进程中途挂掉时，账本里留下的
是真实到达过的状态，而不是事后补写的回忆。

落点沿用既有先例：按父目录派生 `research.db`，**不修改 `src/config.py`**。
"""

from __future__ import annotations

import json
import sqlite3
import threading

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_DB_NAME = "research.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    question       TEXT    NOT NULL,
    state          TEXT    NOT NULL,
    research_rounds INTEGER NOT NULL DEFAULT 0,
    revision_count INTEGER NOT NULL DEFAULT 0,
    critic_score   INTEGER,
    evidence_ids   TEXT    NOT NULL DEFAULT '[]',
    report_path    TEXT    NOT NULL DEFAULT '',
    report_chars   INTEGER NOT NULL DEFAULT 0,
    error          TEXT    NOT NULL DEFAULT '',
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS research_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL,
    seq        INTEGER NOT NULL,
    from_state TEXT    NOT NULL,
    to_state   TEXT    NOT NULL,
    actor      TEXT    NOT NULL,
    reason     TEXT    NOT NULL DEFAULT '',
    at         TEXT    NOT NULL,
    UNIQUE (run_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_research_events_run
    ON research_events (run_id, seq);
"""


class ResearchStoreError(RuntimeError):
    """研究账本操作失败。"""


@dataclass(frozen=True)
class RunRecord:
    id: int
    question: str
    state: str
    research_rounds: int
    revision_count: int
    critic_score: int | None
    evidence_ids: tuple[int, ...]
    report_path: str
    report_chars: int
    error: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class EventRecord:
    run_id: int
    seq: int
    from_state: str
    to_state: str
    actor: str
    reason: str
    at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ResearchStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def init_db(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(_SCHEMA)

    # ---- 运行 -------------------------------------------------------

    def create_run(self, question: str, *, state: str = "planned") -> int:
        if not isinstance(question, str) or not question.strip():
            raise ResearchStoreError("question 不能为空")

        stamp = _now()

        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "INSERT INTO research_runs (question, state, created_at, updated_at) "
                "VALUES (?,?,?,?)",
                (question.strip()[:500], state, stamp, stamp),
            )

            return int(cursor.lastrowid)

    def update_run(
        self,
        run_id: int,
        *,
        state: str | None = None,
        research_rounds: int | None = None,
        revision_count: int | None = None,
        critic_score: int | None = None,
        evidence_ids=None,
        report_path: str | None = None,
        report_chars: int | None = None,
        error: str | None = None,
    ) -> None:
        fields = []
        values = []

        def add(column, value):
            fields.append(f"{column} = ?")
            values.append(value)

        if state is not None:
            add("state", state)

        if research_rounds is not None:
            add("research_rounds", int(research_rounds))

        if revision_count is not None:
            add("revision_count", int(revision_count))

        if critic_score is not None:
            add("critic_score", int(critic_score))

        if evidence_ids is not None:
            add("evidence_ids", json.dumps(list(evidence_ids)))

        if report_path is not None:
            add("report_path", report_path)

        if report_chars is not None:
            add("report_chars", int(report_chars))

        if error is not None:
            add("error", error[:1000])

        add("updated_at", _now())
        values.append(run_id)

        with self._lock:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    f"UPDATE research_runs SET {', '.join(fields)} WHERE id = ?",
                    values,
                )

    def get_run(self, run_id: int) -> RunRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM research_runs WHERE id = ?", (run_id,)
            ).fetchone()

        return _to_run(row) if row is not None else None

    def recent_runs(self, limit: int = 20) -> list[RunRecord]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")

        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM research_runs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()

        return [_to_run(row) for row in rows]

    # ---- 事件 -------------------------------------------------------

    def record_event(
        self,
        run_id: int,
        *,
        seq: int,
        from_state: str,
        to_state: str,
        actor: str,
        reason: str = "",
    ) -> EventRecord:
        for name, value in (
            ("from_state", from_state),
            ("to_state", to_state),
            ("actor", actor),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ResearchStoreError(f"{name} 不能为空")

        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
            raise ResearchStoreError("seq 必须是正整数")

        with self._lock:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    "INSERT INTO research_events "
                    "(run_id, seq, from_state, to_state, actor, reason, at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        run_id,
                        seq,
                        from_state,
                        to_state,
                        actor,
                        reason[:300],
                        _now(),
                    ),
                )

        return EventRecord(
            run_id=run_id,
            seq=seq,
            from_state=from_state,
            to_state=to_state,
            actor=actor,
            reason=reason[:300],
            at=_now(),
        )

    def events(self, run_id: int) -> list[EventRecord]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM research_events WHERE run_id = ? ORDER BY seq",
                (run_id,),
            ).fetchall()

        return [_to_event(row) for row in rows]

    def count_events(self, run_id: int) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM research_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()

        return int(row["n"])


def _to_run(row) -> RunRecord:
    try:
        evidence_ids = tuple(json.loads(row["evidence_ids"]))

    except (TypeError, ValueError):
        evidence_ids = ()

    return RunRecord(
        id=int(row["id"]),
        question=row["question"],
        state=row["state"],
        research_rounds=int(row["research_rounds"]),
        revision_count=int(row["revision_count"]),
        critic_score=(
            None if row["critic_score"] is None else int(row["critic_score"])
        ),
        evidence_ids=evidence_ids,
        report_path=row["report_path"],
        report_chars=int(row["report_chars"]),
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _to_event(row) -> EventRecord:
    return EventRecord(
        run_id=int(row["run_id"]),
        seq=int(row["seq"]),
        from_state=row["from_state"],
        to_state=row["to_state"],
        actor=row["actor"],
        reason=row["reason"],
        at=row["at"],
    )


_STORE = None
_STORE_PATH = None


def get_research_store() -> ResearchStore:
    global _STORE, _STORE_PATH

    from src import config

    db_path = config.paths.chat_requests_db.parent / DEFAULT_DB_NAME

    if _STORE is None or _STORE_PATH != db_path:
        _STORE = ResearchStore(db_path)
        _STORE_PATH = db_path

    return _STORE


def reset_store() -> None:
    global _STORE, _STORE_PATH
    _STORE = None
    _STORE_PATH = None
