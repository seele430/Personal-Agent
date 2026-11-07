"""统一活动时间线：把 spans / execs / artifacts 合成一条按时间倒序的事件流。

三条约束：

1. **只读**：不写任何库；
2. **来源可辨**：每条事件带 source，前端可分组；
3. **不炸**：某个库不存在/字段缺失时跳过该来源，不让整条时间线挂掉。
"""

from __future__ import annotations

import json
import sqlite3

from contextlib import closing
from dataclasses import dataclass, asdict
from pathlib import Path


DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class TimelineError(ValueError):
    """参数不合法。"""


@dataclass(frozen=True)
class TimelineEvent:
    ts: str
    source: str          # span / exec / artifact
    event_id: str
    title: str
    status: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _connect(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None

    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row

        return connection

    except sqlite3.Error:
        return None


def _table_exists(connection, name: str) -> bool:
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()

    return row is not None


def collect_spans(db: Path, *, limit: int) -> list[TimelineEvent]:
    connection = _connect(db)

    if connection is None:
        return []

    with closing(connection):
        if not _table_exists(connection, "spans"):
            return []

        rows = connection.execute(
            "SELECT span_id, name, status, created_at, attrs FROM spans "
            "WHERE created_at IS NOT NULL ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    events = []

    for row in rows:
        detail = ""

        try:
            attrs = json.loads(row["attrs"] or "{}")

        except json.JSONDecodeError:
            attrs = {}

        if isinstance(attrs, dict):
            model = attrs.get("model")

            if model:
                detail = f"model={model}"

        events.append(
            TimelineEvent(
                ts=str(row["created_at"]),
                source="span",
                event_id=str(row["span_id"]),
                title=str(row["name"] or "span"),
                status=str(row["status"] or ""),
                detail=detail,
            )
        )

    return events


def collect_execs(db: Path, *, limit: int) -> list[TimelineEvent]:
    connection = _connect(db)

    if connection is None:
        return []

    with closing(connection):
        if not _table_exists(connection, "platform_execs"):
            return []

        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(platform_execs)")
        }
        ts_column = "created_at" if "created_at" in columns else None

        if ts_column is None:
            return []

        rows = connection.execute(
            f"SELECT id, run_id, state, {ts_column} AS ts FROM platform_execs "
            f"WHERE {ts_column} IS NOT NULL ORDER BY {ts_column} DESC LIMIT ?",
            (limit,),
        ).fetchall()

    return [
        TimelineEvent(
            ts=str(row["ts"]),
            source="exec",
            event_id=str(row["id"]),
            title=f"exec {str(row['run_id'] or '')[:12]}".strip(),
            status=str(row["state"] or ""),
        )
        for row in rows
    ]


def collect_artifacts(db: Path, *, limit: int) -> list[TimelineEvent]:
    connection = _connect(db)

    if connection is None:
        return []

    with closing(connection):
        if not _table_exists(connection, "artifacts"):
            return []

        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(artifacts)")
        }
        ts_column = "created_at" if "created_at" in columns else None

        if ts_column is None:
            return []

        rows = connection.execute(
            f"SELECT id, title, kind, {ts_column} AS ts FROM artifacts "
            f"WHERE {ts_column} IS NOT NULL ORDER BY {ts_column} DESC LIMIT ?",
            (limit,),
        ).fetchall()

    return [
        TimelineEvent(
            ts=str(row["ts"]),
            source="artifact",
            event_id=str(row["id"]),
            title=str(row["title"] or row["kind"] or "artifact"),
            status=str(row["kind"] or ""),
        )
        for row in rows
    ]


def build_timeline(
    *,
    traces_db: str | Path,
    platform_db: str | Path,
    artifacts_db: str | Path,
    limit: int = DEFAULT_LIMIT,
) -> list[TimelineEvent]:
    """合并三个来源，按时间倒序截断。"""

    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TimelineError("limit 必须是整数")

    if not 1 <= limit <= MAX_LIMIT:
        raise TimelineError(f"limit 必须在 1..{MAX_LIMIT}")

    events: list[TimelineEvent] = []
    events += collect_spans(Path(traces_db), limit=limit)
    events += collect_execs(Path(platform_db), limit=limit)
    events += collect_artifacts(Path(artifacts_db), limit=limit)

    events.sort(key=lambda item: item.ts, reverse=True)

    return events[:limit]
