from contextlib import closing
import sqlite3
from datetime import datetime, timedelta, timezone

import requests

from src.core.task_engine import DB_PATH, init_task_db
from src.core.access_policy import decide_access, AccessAction
from src.notification import send_notification


def utc_time_after(seconds=0):
    return (
        datetime.now(timezone.utc) + timedelta(seconds=seconds)
    ).isoformat()


def process_one_notification():
    """从 Outbox 领取并处理一条通知。"""

    init_task_db()
    now = utc_time_after()

    # 使用事务领取任务，避免多个 Worker 同时领取
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            """
            SELECT id, message, attempts
            FROM notification_outbox
            WHERE (
                status = 'pending'
                AND (
                    next_attempt_at IS NULL
                    OR next_attempt_at <= ?
                )
            )
            OR (
                status = 'sending'
                AND next_attempt_at <= ?
            )
            ORDER BY id
            LIMIT 1
            """,
            (now, now)
        ).fetchone()

        if row is None:
            print("没有待发送的通知")
            return False

        notification_id, message, attempts = row

        conn.execute(
            """
            UPDATE notification_outbox
            SET status = 'sending',
                attempts = attempts + 1,
                next_attempt_at = ?
            WHERE id = ?
            """,
            (utc_time_after(120), notification_id)
        )

    # 数据库事务已经提交，开始网络发送
    try:
        send_notification(message)

    except (requests.RequestException, ValueError) as exc:
        next_attempt = attempts + 1
        delay = min(60 * (2 ** min(attempts, 6)), 3600)
        permanent_failure = isinstance(exc, ValueError)

        if isinstance(exc, requests.HTTPError):
            if exc.response is not None:
                decision = decide_access(exc.response.status_code)

                if decision.action == AccessAction.STOP:
                    permanent_failure = True

                delay = max(delay, decision.retry_seconds)

                retry_after = exc.response.headers.get(
                    "Retry-After", ""
                )
                if retry_after.isdigit():
                    delay = max(delay, int(retry_after))

        status = (
            "failed"
            if permanent_failure or next_attempt >= 5
            else "pending"
        )

        with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn, conn:
            conn.execute(
                """
                UPDATE notification_outbox
                SET status = ?,
                    next_attempt_at = ?
                WHERE id = ?
                  AND status = 'sending'
                """,
                (
                    status,
                    utc_time_after(delay) if status == "pending" else None,
                    notification_id
                )
            )

        print(f"通知 {notification_id} 发送失败：{exc}")
        print(f"任务状态：{status}")
        return False

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn, conn:
        conn.execute(
            """
            UPDATE notification_outbox
            SET status = 'sent',
                sent_at = CURRENT_TIMESTAMP,
                next_attempt_at = NULL
            WHERE id = ?
              AND status = 'sending'
            """,
            (notification_id,)
        )

    print(f"通知 {notification_id} 已提交到 ntfy")
    return True


if __name__ == "__main__":
    process_one_notification()