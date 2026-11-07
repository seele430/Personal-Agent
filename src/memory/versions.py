"""记忆版本快照：整库视图的保存与回滚。

为什么"整库视图"而不是"逐条撤销"：

- 回滚要能回答"当时的检索结果是什么"，逐条撤销拼不出这个状态；
- 快照带 SHA256，能把"回滚是否真的回到了那个状态"变成可核对的事实。

回滚是**一个事务里的全量替换**（先清后插，保留原 id），所以回滚后的检索
结果与建快照时逐条一致；`sqlite_sequence` 会同步抬高，避免新条目撞 id。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from src.memory.models import MemoryError, utc_now


SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    label      TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    item_count INTEGER NOT NULL DEFAULT 0,
    sha256     TEXT    NOT NULL,
    payload    TEXT    NOT NULL
);
"""

SNAPSHOT_VERSION = 1


class SnapshotError(MemoryError):
    """快照不存在或内容损坏。"""


@dataclass(frozen=True)
class Snapshot:
    id: int
    label: str
    created_at: str
    item_count: int
    sha256: str


class MemoryVersions:
    def __init__(self, store):
        self.store = store
        self.db_path = Path(store.db_path)
        self.init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def init_db(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(SCHEMA)

    # ---- 创建 -------------------------------------------------------

    def create(self, label: str) -> Snapshot:
        if not isinstance(label, str) or not label.strip():
            raise SnapshotError("快照标签不能为空")

        items = self._dump_items()
        payload = json.dumps(
            {"version": SNAPSHOT_VERSION, "items": items},
            ensure_ascii=False,
            sort_keys=True,
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()

        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "INSERT INTO memory_snapshots (label, created_at, item_count, sha256, payload) "
                "VALUES (?,?,?,?,?)",
                (label.strip()[:120], utc_now(), len(items), digest, payload),
            )

            snapshot_id = int(cursor.lastrowid)

        return Snapshot(
            id=snapshot_id,
            label=label.strip()[:120],
            created_at=utc_now(),
            item_count=len(items),
            sha256=digest,
        )

    def _dump_items(self) -> list[dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM memory_items ORDER BY id"
            ).fetchall()

        return [dict(row) for row in rows]

    # ---- 读取 -------------------------------------------------------

    def list_snapshots(self, limit: int = 20) -> list[Snapshot]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")

        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT id, label, created_at, item_count, sha256 "
                "FROM memory_snapshots ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()

        return [_to_snapshot(row) for row in rows]

    def get(self, snapshot_id: int) -> tuple[Snapshot, list[dict]]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM memory_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()

        if row is None:
            raise SnapshotError(f"快照不存在：{snapshot_id}")

        try:
            payload = json.loads(row["payload"])

        except json.JSONDecodeError as error:
            raise SnapshotError("快照内容不是合法 JSON") from error

        if payload.get("version") != SNAPSHOT_VERSION:
            raise SnapshotError("快照版本不支持")

        items = payload.get("items")

        if not isinstance(items, list):
            raise SnapshotError("快照缺少条目列表")

        digest = hashlib.sha256(row["payload"].encode("utf-8")).hexdigest()

        if digest != row["sha256"]:
            raise SnapshotError("快照哈希不匹配（内容被改动过）")

        return _to_snapshot(row), items

    # ---- 回滚 -------------------------------------------------------

    def restore(self, snapshot_id: int) -> dict:
        """把库恢复到该快照的状态。整个替换在一个事务里完成。"""

        snapshot, items = self.get(snapshot_id)

        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM memory_items_fts")
            connection.execute("DELETE FROM memory_items")

            for item in items:
                _insert_raw(connection, item)

                if item.get("deleted_at") is None:
                    connection.execute(
                        "INSERT INTO memory_items_fts (rowid, text) VALUES (?, ?)",
                        (item["id"], item["text"]),
                    )

            _bump_sequence(connection, max((item["id"] for item in items), default=0))

        return {
            "snapshot_id": snapshot.id,
            "label": snapshot.label,
            "restored": len(items),
            "live": sum(1 for item in items if item.get("deleted_at") is None),
        }

    def diff_counts(self, left_id: int, right_id: int) -> dict:
        """两个快照的条目数差（给界面/日志一个轻量概览）。"""

        left, left_items = self.get(left_id)
        right, right_items = self.get(right_id)

        left_ids = {item["id"] for item in left_items}
        right_ids = {item["id"] for item in right_items}

        return {
            "left": left.id,
            "right": right.id,
            "only_left": len(left_ids - right_ids),
            "only_right": len(right_ids - left_ids),
            "common": len(left_ids & right_ids),
        }


_ITEM_COLUMNS = (
    "id",
    "owner_id",
    "kind",
    "layer",
    "trust",
    "key",
    "value",
    "text",
    "source_ref",
    "source_doc_id",
    "valid_from",
    "valid_to",
    "supersedes_id",
    "conflict_group",
    "created_at",
    "updated_at",
    "deleted_at",
)


def _insert_raw(connection, item: dict) -> None:
    missing = [column for column in _ITEM_COLUMNS if column not in item]

    if missing:
        raise SnapshotError(f"快照条目缺少字段：{missing}")

    placeholders = ",".join("?" for _ in _ITEM_COLUMNS)

    connection.execute(
        f"INSERT INTO memory_items ({','.join(_ITEM_COLUMNS)}) "
        f"VALUES ({placeholders})",
        [item[column] for column in _ITEM_COLUMNS],
    )


def _bump_sequence(connection, highest: int) -> None:
    connection.execute(
        "UPDATE sqlite_sequence SET seq = ? WHERE name = 'memory_items' "
        "AND seq < ?",
        (highest, highest),
    )


def _to_snapshot(row) -> Snapshot:
    return Snapshot(
        id=int(row["id"]),
        label=row["label"],
        created_at=row["created_at"],
        item_count=int(row["item_count"]),
        sha256=row["sha256"],
    )
