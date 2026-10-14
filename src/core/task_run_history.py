
import logging
import sqlite3

from contextlib import closing
from datetime import datetime, timezone
from uuid import uuid4

from src.config import paths


logger = logging.getLogger("personal_agent.history")


def utc_now():
    return datetime.now(timezone.utc)


# =====================================
# Initialize Run History Database
# =====================================

def init_run_history() -> bool:
    """初始化持久化任务执行历史表。"""

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_run_history (
                    run_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    lease_token TEXT,
                    status TEXT NOT NULL
                        CHECK (
                            status IN (
                                'running',
                                'success',
                                'failed'
                            )
                        ),
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    duration_ms INTEGER,
                    new_items INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_run_history_task
                ON task_run_history (
                    task_id,
                    started_at DESC
                )
            """)

    return True


# =====================================
# Start Task Execution
# =====================================

def start_task_run(
    task_id: str,
    lease_token: str | None = None
) -> str:
    """记录任务开始执行。"""

    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task_id 不能为空")

    init_run_history()

    run_id = "run_" + uuid4().hex

    started_at = utc_now().isoformat()

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        with conn:
            conn.execute(
                """
                INSERT INTO task_run_history (
                    run_id,
                    task_id,
                    lease_token,
                    status,
                    started_at
                )
                VALUES (?, ?, ?, 'running', ?)
                """,
                (
                    run_id,
                    task_id,
                    lease_token,
                    started_at
                )
            )

    logger.info(
        "任务开始 task_id=%s run_id=%s",
        task_id,
        run_id
    )

    return run_id


# =====================================
# Finish Task Execution
# =====================================

def finish_task_run(
    run_id: str,
    status: str,
    new_items: int = 0,
    error_message: str | None = None
) -> bool:
    """记录任务完成结果及耗时。"""

    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id 不能为空")

    if status not in ("success", "failed"):
        raise ValueError("status 只能是 success 或 failed")

    if (
        isinstance(new_items, bool)
        or not isinstance(new_items, int)
        or new_items < 0
    ):
        raise ValueError("new_items 必须是非负整数")

    init_run_history()

    finished_at = utc_now()

    error_text = None

    if status == "failed":
        error_text = str(
            error_message or "Unknown error"
        )[:500]

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        with conn:
            conn.execute("BEGIN IMMEDIATE")

            row = conn.execute(
                """
                SELECT started_at
                FROM task_run_history
                WHERE run_id = ?
                  AND status = 'running'
                """,
                (run_id,)
            ).fetchone()

            if row is None:
                return False

            started_at = datetime.fromisoformat(row[0])

            duration_ms = max(
                0,
                round(
                    (finished_at - started_at).total_seconds()
                    * 1000
                )
            )

            cursor = conn.execute(
                """
                UPDATE task_run_history
                SET status = ?,
                    finished_at = ?,
                    duration_ms = ?,
                    new_items = ?,
                    error_message = ?
                WHERE run_id = ?
                  AND status = 'running'
                """,
                (
                    status,
                    finished_at.isoformat(),
                    duration_ms,
                    new_items,
                    error_text,
                    run_id
                )
            )

            updated = cursor.rowcount == 1

    if updated:
        logger.info(
            "任务结束 run_id=%s status=%s "
            "duration_ms=%s new_items=%s",
            run_id,
            status,
            duration_ms,
            new_items
        )

    return updated


# =====================================
# Query Task Execution History
# =====================================

def list_task_runs(
    task_id: str,
    limit: int = 20
) -> list[dict]:
    """查询指定任务的执行历史。"""

    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task_id 不能为空")

    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 100
    ):
        raise ValueError("limit 必须在 1 到 100 之间")

    init_run_history()

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute(
            """
            SELECT
                run_id,
                task_id,
                status,
                started_at,
                finished_at,
                duration_ms,
                new_items,
                error_message
            FROM task_run_history
            WHERE task_id = ?
            ORDER BY started_at DESC, run_id DESC
            LIMIT ?
            """,
            (task_id, limit)
        ).fetchall()

        return [dict(row) for row in rows]