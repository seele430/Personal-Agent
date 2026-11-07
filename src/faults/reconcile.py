"""对账：把"账上有、盘上没有"和"卡住不动"的记录找出来并收敛。

两条铁律：

1. **只收敛能证明不可用的**：产物行对应的文件确实不存在时才算坏；文件在
   但没登记（孤儿文件）**只报告，不删除**——删文件是不可逆动作。
2. **不碰未接入的东西**：只处理本系统登记过的产物与 Outbox 行。
"""

from __future__ import annotations

import sqlite3

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class ArtifactReconcile:
    checked: int
    missing: tuple[str, ...]
    orphan_files: tuple[str, ...]
    purged: int

    def to_dict(self) -> dict:
        return {
            "checked": self.checked,
            "missing": list(self.missing),
            "orphan_files": list(self.orphan_files),
            "purged": self.purged,
        }


@dataclass(frozen=True)
class OutboxReconcile:
    stuck: tuple[int, ...]
    requeued: int

    def to_dict(self) -> dict:
        return {"stuck": list(self.stuck), "requeued": self.requeued}


def reconcile_artifacts(registry, *, purge_missing: bool = False) -> ArtifactReconcile:
    """检查产物行与磁盘文件是否一致。

    `purge_missing=False`（默认）只报告；显式开启才删除"文件已不存在"的行。
    """

    rows = registry.list_all_for_audit() if hasattr(registry, "list_all_for_audit") else None

    if rows is None:
        rows = _all_artifact_rows(registry)

    missing = []
    orphan_candidates = set()
    purged = 0

    for row in rows:
        path = registry.root / row["rel_path"]

        if not path.is_file():
            missing.append(row["id"])

            if purge_missing:
                registry.delete(row["id"], owner_id=row["owner_id"])
                purged += 1

        else:
            orphan_candidates.add(row["rel_path"])

    on_disk = {
        item.relative_to(registry.root).as_posix()
        for item in registry.root.rglob("*")
        if item.is_file()
    }

    orphans = tuple(sorted(on_disk - orphan_candidates))

    return ArtifactReconcile(
        checked=len(rows),
        missing=tuple(missing),
        orphan_files=orphans,
        purged=purged,
    )


def _all_artifact_rows(registry) -> list[dict]:
    with closing(sqlite3.connect(registry.db_path, timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT id, owner_id, rel_path FROM artifacts ORDER BY id"
        ).fetchall()

    return [dict(row) for row in rows]


def reconcile_outbox(
    tasks_db: str | Path,
    *,
    stuck_after_seconds: int = 300,
    now: datetime | None = None,
) -> OutboxReconcile:
    """把卡在 `sending` 且已过重试时间的通知拉回 `pending`。"""

    if isinstance(stuck_after_seconds, bool) or not isinstance(stuck_after_seconds, int):
        raise ValueError("stuck_after_seconds 必须是整数")

    if stuck_after_seconds < 0:
        raise ValueError("stuck_after_seconds 不能为负")

    moment = now or datetime.now(timezone.utc)
    cutoff = (moment - timedelta(seconds=stuck_after_seconds)).isoformat()

    database = Path(tasks_db)

    if not database.exists():
        return OutboxReconcile(stuck=(), requeued=0)

    with closing(sqlite3.connect(database, timeout=10)) as connection:
        connection.row_factory = sqlite3.Row

        try:
            rows = connection.execute(
                "SELECT id FROM notification_outbox WHERE status = 'sending' "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= ?)",
                (cutoff,),
            ).fetchall()

        except sqlite3.OperationalError:
            return OutboxReconcile(stuck=(), requeued=0)

    stuck = tuple(int(row["id"]) for row in rows)

    if not stuck:
        return OutboxReconcile(stuck=(), requeued=0)

    with closing(sqlite3.connect(database, timeout=10)) as connection, connection:
        connection.execute(
            "UPDATE notification_outbox SET status = 'pending', next_attempt_at = NULL "
            "WHERE status = 'sending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?)",
            (cutoff,),
        )

    return OutboxReconcile(stuck=stuck, requeued=len(stuck))
