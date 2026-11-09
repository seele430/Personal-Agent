"""旧版数据库到 run / turn / step 模型的备份、迁移与恢复。

只新增表和行，不 ALTER / UPDATE / DELETE bots、rooms 或 messages。
调用方必须传入旧库的临时副本，不能直接指向真实数据目录。
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.run_store import init_run_schema


REQUIRED_COLUMNS = {
    "rooms": {"id", "owner_id", "created_at"},
    "messages": {
        "id", "room_id", "turn_id", "speaker", "bot_id",
        "content", "created_at",
    },
    "bots": {"id", "name"},
}


class MigrationError(RuntimeError):
    """迁移输入、幂等冲突或恢复校验失败。"""


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _check_integrity(conn: sqlite3.Connection) -> None:
    result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise MigrationError(f"SQLite 完整性检查失败：{result}")


def _validate_legacy_schema(conn: sqlite3.Connection) -> None:
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    for table, required in REQUIRED_COLUMNS.items():
        if table not in tables:
            raise MigrationError(f"缺少旧表：{table}")
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        missing = required - columns
        if missing:
            raise MigrationError(f"旧表 {table} 缺少字段：{sorted(missing)}")


def _snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple[Any, ...]]]:
    """Capture legacy rows by value, including the old stage columns."""

    _validate_legacy_schema(conn)
    return {
        table: [
            tuple(row)
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")
        ]
        for table in ("bots", "rooms", "messages")
    }


def _iso_timestamp(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise MigrationError(f"无效旧时间戳：{value!r}") from exc


def create_backup(db_path: str | Path, backup_dir: str | Path) -> Path:
    """Create a consistent online SQLite backup without modifying the source."""

    source_path = Path(db_path).resolve()
    backup_root = Path(backup_dir).resolve()
    if not source_path.is_file():
        raise MigrationError(f"旧库副本不存在：{source_path}")
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    backup_path = backup_root / (
        f"{source_path.stem}-pre-run-model-{stamp}-{uuid.uuid4().hex[:8]}.db"
    )

    with closing(_connect(source_path)) as source:
        _check_integrity(source)
        before = _snapshot(source)
        with closing(_connect(backup_path)) as backup:
            source.backup(backup)
            _check_integrity(backup)
            if _snapshot(backup) != before:
                raise MigrationError("备份与旧库副本的旧表数据不一致")
    return backup_path


def restore_backup(
    db_path: str | Path,
    backup_path: str | Path,
    *,
    expected_snapshot: dict[str, list[tuple[Any, ...]]] | None = None,
) -> dict[str, int]:
    """Restore the complete copy and verify legacy rows are unchanged."""

    target_path = Path(db_path).resolve()
    source_path = Path(backup_path).resolve()
    if not source_path.is_file():
        raise MigrationError(f"备份不存在：{source_path}")
    if target_path == source_path:
        raise MigrationError("恢复目标不能与备份相同")

    with closing(_connect(source_path)) as source:
        _check_integrity(source)
        backup_snapshot = _snapshot(source)
        expected = expected_snapshot or backup_snapshot
        if backup_snapshot != expected:
            raise MigrationError("备份的旧表数据与迁移前快照不一致")
        with closing(_connect(target_path)) as target:
            source.backup(target)
            _check_integrity(target)
            if _snapshot(target) != expected:
                raise MigrationError("恢复后的旧表数据与迁移前快照不一致")

    with closing(_connect(target_path)) as target:
        remaining = {
            row[0]
            for row in target.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name IN (
                    'agent_runs', 'agent_turns', 'agent_steps',
                    'agent_execs', 'run_model_meta'
                )
                """
            )
        }
    if remaining:
        raise MigrationError(f"恢复后仍存在新模型表：{sorted(remaining)}")

    return {table: len(rows) for table, rows in expected.items()}


def _expect_absent_or_equal(
    conn: sqlite3.Connection,
    table: str,
    row_id: str,
    expected: dict[str, Any],
) -> bool:
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        return True
    actual = dict(row)
    if any(actual[key] != value for key, value in expected.items()):
        raise MigrationError(f"幂等冲突：{table}.{row_id} 已存在但内容不同")
    return False


def migrate_legacy_database(
    db_path: str | Path,
    backup_dir: str | Path,
) -> dict[str, Any]:
    """Migrate a legacy copy atomically; any failure restores the backup."""

    source_path = Path(db_path).resolve()
    backup_path = create_backup(source_path, backup_dir)
    with closing(_connect(source_path)) as conn:
        original = _snapshot(conn)

    try:
        counts = {"runs_created": 0, "turns_created": 0, "steps_created": 0}
        with closing(_connect(source_path)) as conn:
            _check_integrity(conn)
            if _snapshot(conn) != original:
                raise MigrationError("迁移前旧表数据发生变化")
            conn.execute("BEGIN IMMEDIATE")
            try:
                init_run_schema(conn)
                forged = conn.execute(
                    """
                    SELECT COUNT(*) FROM agent_execs e
                    JOIN agent_steps s ON s.id = e.step_id
                    WHERE s.source_message_id IS NOT NULL
                    """
                ).fetchone()[0]
                if forged:
                    raise MigrationError("历史消息不允许关联工具 exec")

                bot_names = {
                    row[0]: row[1]
                    for row in conn.execute("SELECT id, name FROM bots")
                }
                rooms = conn.execute(
                    "SELECT * FROM rooms ORDER BY created_at, id"
                ).fetchall()
                for room in rooms:
                    run_id = f"legacy-room-{room['id']}"
                    created_at = _iso_timestamp(room["created_at"])
                    run = {
                        "id": run_id,
                        "request_id": None,
                        "owner_id": room["owner_id"],
                        "status": "succeeded",
                        "created_at": created_at,
                        "updated_at": created_at,
                    }
                    if _expect_absent_or_equal(conn, "agent_runs", run_id, run):
                        conn.execute(
                            """
                            INSERT INTO agent_runs(
                                id, request_id, owner_id, status,
                                created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            tuple(run.values()),
                        )
                        counts["runs_created"] += 1

                    messages = conn.execute(
                        """
                        SELECT * FROM messages WHERE room_id = ?
                        ORDER BY created_at, id
                        """,
                        (room["id"],),
                    ).fetchall()
                    turn_ids = list(dict.fromkeys(row["turn_id"] for row in messages))
                    for sequence, turn_id in enumerate(turn_ids, start=1):
                        turn_messages = [
                            row for row in messages if row["turn_id"] == turn_id
                        ]
                        first_at = _iso_timestamp(turn_messages[0]["created_at"])
                        user_message = next(
                            (
                                row["content"]
                                for row in turn_messages
                                if row["speaker"] == "user"
                            ),
                            "",
                        )
                        turn = {
                            "id": turn_id,
                            "run_id": run_id,
                            "sequence": sequence,
                            "user_message": user_message,
                            "status": "succeeded",
                            "created_at": first_at,
                        }
                        if _expect_absent_or_equal(conn, "agent_turns", turn_id, turn):
                            conn.execute(
                                """
                                INSERT INTO agent_turns(
                                    id, run_id, sequence, user_message,
                                    status, created_at
                                ) VALUES (?, ?, ?, ?, ?, ?)
                                """,
                                tuple(turn.values()),
                            )
                            counts["turns_created"] += 1

                        for step_sequence, message in enumerate(turn_messages, start=1):
                            is_user = message["speaker"] == "user"
                            bot_id = message["bot_id"]
                            step = {
                                "id": f"legacy-message-{message['id']}",
                                "turn_id": turn_id,
                                "sequence": step_sequence,
                                "kind": "user" if is_user else "assistant",
                                "actor": (
                                    "user" if is_user
                                    else f"bot:{bot_id}" if bot_id is not None
                                    else "bot:deleted"
                                ),
                                "actor_name": (
                                    None if is_user else bot_names.get(bot_id)
                                ),
                                "content": message["content"],
                                "status": "succeeded",
                                "source_message_id": message["id"],
                                "created_at": _iso_timestamp(message["created_at"]),
                            }
                            if _expect_absent_or_equal(conn, "agent_steps", step["id"], step):
                                conn.execute(
                                    """
                                    INSERT INTO agent_steps(
                                        id, turn_id, sequence, kind, actor,
                                        actor_name, content, status,
                                        source_message_id, created_at
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                    """,
                                    tuple(step.values()),
                                )
                                counts["steps_created"] += 1

                if _snapshot(conn) != original:
                    raise MigrationError("迁移修改了受保护的旧表")
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        with closing(_connect(source_path)) as conn:
            _check_integrity(conn)
            if _snapshot(conn) != original:
                raise MigrationError("迁移提交后旧表数据发生变化")
        return {
            "status": "migrated",
            "database": str(source_path),
            "backup": str(backup_path),
            "rooms": len(original["rooms"]),
            "messages": len(original["messages"]),
            **counts,
        }
    except Exception:
        restore_backup(source_path, backup_path, expected_snapshot=original)
        raise
