"""数据生命周期：按保留期清理旧行，默认只预演、不删。

护栏：

1. **默认 dry-run**：`plan()` 只算候选；
2. **删必须显式确认**：`apply(plan, confirm=True)` 才动手；
3. **保留最近 N 条**：即使很旧也不删最近 N 条（防止把"当前正在用的"删掉）；
4. **表名白名单**：只允许 `spans` / `platform_execs`，不许拼任意表名；
5. **只删行不删文件**：产物文件由用户自己处理。
"""

from __future__ import annotations

import sqlite3

from contextlib import closing
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


#: 允许清理的表 → 时间列
ALLOWED_TABLES = {
    "spans": "created_at",
    "platform_execs": "created_at",
}


class RetentionError(ValueError):
    """生命周期参数不合法。"""


@dataclass(frozen=True)
class RetentionPolicy:
    days: int = 30
    keep_last: int = 50

    def __post_init__(self):
        if isinstance(self.days, bool) or not isinstance(self.days, int) or self.days < 1:
            raise RetentionError("days 必须是 ≥1 的整数")

        if (
            isinstance(self.keep_last, bool)
            or not isinstance(self.keep_last, int)
            or self.keep_last < 0
        ):
            raise RetentionError("keep_last 必须是非负整数")


@dataclass(frozen=True)
class PurgeTarget:
    table: str
    cutoff: str
    candidates: tuple[str, ...]
    kept_recent: int

    @property
    def count(self) -> int:
        return len(self.candidates)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["count"] = self.count

        return payload


@dataclass(frozen=True)
class PurgePlan:
    targets: tuple[PurgeTarget, ...] = ()
    applied: bool = False
    deleted: int = 0

    @property
    def total(self) -> int:
        return sum(target.count for target in self.targets)

    def to_dict(self) -> dict:
        return {
            "targets": [target.to_dict() for target in self.targets],
            "total": self.total,
            "applied": self.applied,
            "deleted": self.deleted,
        }


def _open(db: str | Path) -> sqlite3.Connection:
    path = Path(db)

    if not path.is_file():
        raise RetentionError(f"数据库不存在：{path}")

    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row

    return connection


def plan_table(
    db: str | Path,
    table: str,
    *,
    policy: RetentionPolicy,
    now: datetime | None = None,
) -> PurgeTarget:
    """算一张表的候选行（不动数据）。"""

    if table not in ALLOWED_TABLES:
        raise RetentionError(f"不允许清理的表：{table}")

    column = ALLOWED_TABLES[table]
    moment = now or datetime.now(timezone.utc)
    cutoff = (moment - timedelta(days=policy.days)).isoformat()

    with closing(_open(db)) as connection:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}

        if column not in columns:
            raise RetentionError(f"表 {table} 缺少时间列 {column}")

        rows = connection.execute(
            f"SELECT rowid, {column} AS ts FROM {table} "
            f"WHERE {column} IS NOT NULL AND {column} < ? "
            f"ORDER BY {column} DESC",
            (cutoff,),
        ).fetchall()

    recent_ids = {str(row["rowid"]) for row in rows[: policy.keep_last]}
    candidates = tuple(
        str(row["rowid"]) for row in rows if str(row["rowid"]) not in recent_ids
    )

    return PurgeTarget(
        table=table,
        cutoff=cutoff,
        candidates=candidates,
        kept_recent=min(policy.keep_last, len(rows)),
    )


def plan(
    *,
    spans_db: str | Path | None = None,
    platform_db: str | Path | None = None,
    policy: RetentionPolicy,
    now: datetime | None = None,
) -> PurgePlan:
    targets = []

    if spans_db:
        targets.append(plan_table(spans_db, "spans", policy=policy, now=now))

    if platform_db:
        targets.append(plan_table(platform_db, "platform_execs", policy=policy, now=now))

    return PurgePlan(targets=tuple(targets))


def apply(
    plan_value: PurgePlan,
    *,
    spans_db: str | Path | None = None,
    platform_db: str | Path | None = None,
    confirm: bool = False,
) -> PurgePlan:
    """执行清理；`confirm=True` 才真删。"""

    if not confirm:
        raise RetentionError("删除前必须显式确认：apply(..., confirm=True)")

    databases = {"spans": spans_db, "platform_execs": platform_db}
    deleted = 0

    for target in plan_value.targets:
        db = databases.get(target.table)

        if db is None or not target.candidates:
            continue

        with closing(_open(db)) as connection, connection:
            placeholders = ",".join("?" for _ in target.candidates)
            deleted += connection.execute(
                f"DELETE FROM {target.table} WHERE rowid IN ({placeholders})",
                target.candidates,
            ).rowcount

    return PurgePlan(targets=plan_value.targets, applied=True, deleted=deleted)
