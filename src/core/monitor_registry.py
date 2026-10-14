
import sqlite3

from contextlib import closing
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from src.config import paths, init_task_db


# =====================================
# Database Initialization
# =====================================

def init_monitor_registry() -> bool:
    """初始化监控任务表，并兼容旧数据库。"""

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS monitor_tasks (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    interval_minutes INTEGER NOT NULL
                        CHECK (interval_minutes >= 15),
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'paused')),
                    last_checked_at TEXT,
                    next_run_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,
                    archived_at TEXT
                )
            """)

            columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(monitor_tasks)"
                )
            }

            if "archived_at" not in columns:
                conn.execute(
                    "ALTER TABLE monitor_tasks "
                    "ADD COLUMN archived_at TEXT"
                )

    return True


# =====================================
# Input Validation
# =====================================

def validate_owner_task(owner_id: str, task_id: str):
    """验证任务所属用户与任务 ID 的基本格式。"""

    if not isinstance(owner_id, str) or not owner_id.strip():
        raise ValueError("owner_id 不能为空")

    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task_id 不能为空")


# =====================================
# Create Task
# =====================================

def create_monitor_task(
    owner_id: str,
    source_url: str,
    interval_minutes: int = 30
) -> str:
    """创建 RSS 监控任务。"""

    if not isinstance(owner_id, str) or not owner_id.strip():
        raise ValueError("owner_id 不能为空")

    if (
        isinstance(interval_minutes, bool)
        or not isinstance(interval_minutes, int)
        or interval_minutes < 15
    ):
        raise ValueError("检查间隔必须是至少 15 分钟的整数")

    # 当前开发版仅支持已验证的官方 RSS
    if source_url != "https://blog.python.org/rss.xml":
        raise ValueError("当前仅支持 Python 官方博客 RSS")

    task_id = "rss_" + uuid4().hex

    next_run = (
        datetime.now(timezone.utc)
        + timedelta(minutes=interval_minutes)
    ).isoformat()

    init_monitor_registry()

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        with conn:
            conn.execute(
                """
                INSERT INTO monitor_tasks (
                    id,
                    owner_id,
                    task_type,
                    source_url,
                    interval_minutes,
                    status,
                    next_run_at
                )
                VALUES (?, ?, 'rss', ?, ?, 'active', ?)
                """,
                (
                    task_id,
                    owner_id,
                    source_url,
                    interval_minutes,
                    next_run
                )
            )

    return task_id


# =====================================
# List Tasks
# =====================================

def list_monitor_tasks(owner_id: str) -> list[dict]:
    """查询用户尚未归档的监控任务。"""

    if not isinstance(owner_id, str) or not owner_id.strip():
        raise ValueError("owner_id 不能为空")

    init_monitor_registry()

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute(
            """
            SELECT
                id,
                owner_id,
                task_type,
                source_url,
                interval_minutes,
                status,
                last_checked_at,
                next_run_at,
                last_error,
                created_at,
                archived_at
            FROM monitor_tasks
            WHERE owner_id = ?
              AND archived_at IS NULL
            ORDER BY created_at DESC, id DESC
            """,
            (owner_id,)
        ).fetchall()

        return [dict(row) for row in rows]


# =====================================
# Pause Task
# =====================================

def pause_monitor_task(
    owner_id: str,
    task_id: str
) -> bool:
    """暂停监控任务。"""

    validate_owner_task(owner_id, task_id)
    init_monitor_registry()

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        with conn:
            cursor = conn.execute(
                """
                UPDATE monitor_tasks
                SET status = 'paused'
                WHERE id = ?
                  AND owner_id = ?
                  AND status = 'active'
                  AND archived_at IS NULL
                """,
                (task_id, owner_id)
            )

            return cursor.rowcount == 1


# =====================================
# Resume Task
# =====================================

def resume_monitor_task(
    owner_id: str,
    task_id: str
) -> bool:
    """恢复已暂停的任务。"""

    validate_owner_task(owner_id, task_id)
    init_monitor_registry()

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        with conn:
            cursor = conn.execute(
                """
                UPDATE monitor_tasks
                SET status = 'active'
                WHERE id = ?
                  AND owner_id = ?
                  AND status = 'paused'
                  AND archived_at IS NULL
                """,
                (task_id, owner_id)
            )

            return cursor.rowcount == 1


# =====================================
# Soft Delete / Archive Task
# =====================================

def archive_monitor_task(
    owner_id: str,
    task_id: str
) -> bool:
    """
    软删除监控任务。

    1. 检查用户是否拥有此任务
    2. 将任务状态设置为 paused
    3. 记录归档时间
    4. 清除下一次调度时间
    5. 取消尚未发送的 Outbox 通知

    不删除历史任务数据。
    """

    validate_owner_task(owner_id, task_id)

    init_monitor_registry()
    init_task_db()

    archived_at = datetime.now(timezone.utc).isoformat()

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        with conn:
            conn.execute("BEGIN IMMEDIATE")

            cursor = conn.execute(
                """
                UPDATE monitor_tasks
                SET status = 'paused',
                    archived_at = ?,
                    next_run_at = NULL
                WHERE id = ?
                  AND owner_id = ?
                  AND archived_at IS NULL
                """,
                (
                    archived_at,
                    task_id,
                    owner_id
                )
            )

            if cursor.rowcount != 1:
                return False

            # 取消该任务尚未发送的通知。
            # 已经在发送中的通知无法保证撤回。
            conn.execute(
                """
                UPDATE notification_outbox
                SET status = 'cancelled',
                    next_attempt_at = NULL
                WHERE monitor_id = ?
                  AND status = 'pending'
                """,
                (task_id,)
            )

    return True