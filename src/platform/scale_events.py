"""弹性扩缩容事件：持久化 + 动作语义到页面标签的映射。

口径（W3 复核 R8）：落库的只有**容量真的变了**的事件 —— ``scale_up`` /
``scale_down``。``hold`` 类（等待稳定期、冷却中、稳定）属于**决策**，
由 ``ElasticController`` 以内存快照提供，不进这张表，否则 30 秒一次 tick
会变成每天几千行无信息量的记录。

``at`` 一律是墙钟时间（``src.platform.autoscale`` 的 wall_fn），不是单调时钟。
"""

from __future__ import annotations

import sqlite3
import uuid

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from src import config

ACTION_SCALE_UP = "scale_up"
ACTION_SCALE_DOWN = "scale_down"
ACTION_HOLD = "none"

CHANGE_ACTIONS = (ACTION_SCALE_UP, ACTION_SCALE_DOWN)

REASON_LOAD_ABOVE = "load_above_capacity"
REASON_LOAD_BELOW = "load_below_capacity"
REASON_PENDING = "scale_up_pending"
REASON_COOLDOWN = "scale_down_cooldown"
REASON_STEADY = "steady"

CAPACITY_UNIT_NODES = "nodes"

SOURCE_LIVE = "live"
SOURCE_SIMULATION = "simulation"

SOURCES = (SOURCE_LIVE, SOURCE_SIMULATION)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_scale_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_scale_events (
            id TEXT PRIMARY KEY,
            at TEXT NOT NULL,
            action TEXT NOT NULL CHECK (action IN ('scale_up', 'scale_down')),
            reason TEXT NOT NULL,
            before_capacity INTEGER NOT NULL,
            after_capacity INTEGER NOT NULL,
            running INTEGER NOT NULL DEFAULT 0,
            queued INTEGER NOT NULL DEFAULT 0,
            capacity_unit TEXT NOT NULL DEFAULT 'nodes',
            source TEXT NOT NULL CHECK (source IN ('live', 'simulation')),
            controller_run_id TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_scale_events_at
        ON platform_scale_events (at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_scale_events_source
        ON platform_scale_events (source, at)
        """
    )


def describe(action: str, reason: str) -> dict[str, str]:
    """把代码里的 ``(action, reason)`` 翻成页面语义。

    返回 ``label``（scale_out / scale_in / hold）、``detail``、``title``。
    未知组合原样透出，绝不静默猜成扩容。
    """

    if action == ACTION_SCALE_UP:
        return {"label": "scale_out", "detail": "capacity", "title": "扩容"}

    if action == ACTION_SCALE_DOWN:
        return {"label": "scale_in", "detail": "capacity", "title": "缩容"}

    if action != ACTION_HOLD:
        return {"label": "unknown", "detail": reason, "title": reason}

    if reason.startswith(REASON_PENDING):
        return {"label": "hold", "detail": "pending", "title": "等待稳定期"}

    if reason == REASON_COOLDOWN:
        return {"label": "hold", "detail": "cooldown", "title": "冷却中"}

    if reason == REASON_STEADY:
        return {"label": "hold", "detail": "steady", "title": "稳定"}

    return {"label": "hold", "detail": reason, "title": reason}


def is_change(action: str) -> bool:
    """只有这两个动作代表真实容量变化。"""

    return action in CHANGE_ACTIONS


class ScaleEventStore:
    """``platform_scale_events`` 的读写；只接受真实变更类动作。"""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or config.paths.platform_db)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with closing(self.connect()) as conn, conn:
            init_scale_schema(conn)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def record(
        self,
        *,
        action: str,
        reason: str,
        before_capacity: int,
        after_capacity: int,
        running: int = 0,
        queued: int = 0,
        capacity_unit: str = CAPACITY_UNIT_NODES,
        source: str = SOURCE_LIVE,
        controller_run_id: str | None = None,
        at: str | None = None,
    ) -> dict:
        if not is_change(action):
            raise ValueError(
                f"只记录真实容量变化事件，收到 action={action!r}；"
                "hold 类决策请走控制器内存快照"
            )

        if source not in SOURCES:
            raise ValueError(f"未知 source={source!r}")

        if not capacity_unit:
            raise ValueError("capacity_unit 不能为空")

        if int(before_capacity) < 0 or int(after_capacity) < 0:
            raise ValueError("capacity 不能为负")

        row = {
            "id": uuid.uuid4().hex,
            "at": at or utc_now(),
            "action": action,
            "reason": reason,
            "before_capacity": int(before_capacity),
            "after_capacity": int(after_capacity),
            "running": int(running),
            "queued": int(queued),
            "capacity_unit": capacity_unit,
            "source": source,
            "controller_run_id": controller_run_id,
        }

        with closing(self.connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO platform_scale_events(
                    id, at, action, reason, before_capacity, after_capacity,
                    running, queued, capacity_unit, source, controller_run_id
                ) VALUES (
                    :id, :at, :action, :reason, :before_capacity,
                    :after_capacity, :running, :queued, :capacity_unit,
                    :source, :controller_run_id
                )
                """,
                row,
            )

        return self.describe_row(row)

    @staticmethod
    def describe_row(row) -> dict:
        data = dict(row) if not isinstance(row, dict) else dict(row)
        data.update(describe(str(data["action"]), str(data["reason"])))
        return data

    def recent(
        self,
        limit: int = 20,
        *,
        source: str | None = None,
    ) -> list[dict]:
        limit = max(0, min(int(limit), 200))

        if limit == 0:
            return []

        with closing(self.connect()) as conn:
            if source is None:
                rows = conn.execute(
                    """
                    SELECT * FROM platform_scale_events
                    ORDER BY at DESC, rowid DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM platform_scale_events
                    WHERE source = ?
                    ORDER BY at DESC, rowid DESC
                    LIMIT ?
                    """,
                    (source, limit),
                ).fetchall()

        return [self.describe_row(row) for row in rows]

    def count(self, *, source: str | None = None) -> int:
        with closing(self.connect()) as conn:
            if source is None:
                row = conn.execute(
                    "SELECT COUNT(*) FROM platform_scale_events"
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM platform_scale_events WHERE source = ?",
                    (source,),
                ).fetchone()

        return int(row[0]) if row else 0
