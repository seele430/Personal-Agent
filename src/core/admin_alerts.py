
import hashlib
import json
import logging
import sqlite3

from contextlib import closing
from datetime import datetime, timedelta, timezone

from src.core.task_engine import DB_PATH
from src.core.task_health import scan_task_health


logger = logging.getLogger("personal_agent.admin_alerts")

ALERT_TYPES = {
    "repeated_failures": "任务连续失败",
    "paused_error": "任务因异常暂停",
    "suspected_stuck_run": "疑似任务卡住",
}

DEFAULT_COOLDOWN_SECONDS = 3600


def utc_now():
    return datetime.now(timezone.utc)


def init_admin_alert_db() -> bool:
    """初始化管理员告警状态与待发送队列。"""

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS admin_alert_state (
                    issue_key TEXT PRIMARY KEY,
                    alert_type TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    run_id TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_enqueued_at TEXT,
                    sightings INTEGER NOT NULL DEFAULT 0
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS admin_alert_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_key TEXT NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (
                            status IN (
                                'pending',
                                'sending',
                                'sent',
                                'failed'
                            )
                        ),
                    created_at TEXT NOT NULL,
                    sent_at TEXT
                )
            """)

            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_admin_alert_pending
                ON admin_alert_outbox (issue_key)
                WHERE status = 'pending'
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS
                    idx_admin_alert_status
                ON admin_alert_outbox (status, created_at)
            """)

    return True


def get_issue_identity(issue: dict):
    """生成稳定的故障指纹。"""

    issue_type = issue.get("type")
    task_id = issue.get("task_id")
    run_id = issue.get("run_id")

    if issue_type not in ALERT_TYPES:
        return None

    if not isinstance(task_id, str) or not task_id:
        return None

    # 同一个 stuck run 使用同一个告警标识
    if issue_type == "suspected_stuck_run":
        if not isinstance(run_id, str) or not run_id:
            return None
    else:
        run_id = None

    identity = {
        "type": issue_type,
        "task_id": task_id,
        "run_id": run_id,
    }

    raw = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
    )

    issue_key = hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()

    return issue_key, issue_type, task_id, run_id


def build_alert_message(
    issue_type: str,
    task_id: str,
) -> str:
    """
    生成告警摘要。

    不包含 API Key、网络异常原文、
    用户聊天内容或用户上传文件内容。
    """

    title = ALERT_TYPES[issue_type]

    return (
        f"Personal Agent 管理员告警\n"
        f"异常类型：{title}\n"
        f"任务 ID：{task_id}\n"
        f"请到管理员后台查看详细情况。"
    )


def enqueue_health_alerts(
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
) -> int:
    """
    扫描任务健康状态并写入告警队列。

    同一个故障：
    - 存在 pending 告警时不重复入队
    - 冷却时间未结束时不重复入队
    - 冷却结束且上一条已处理时可重新入队

    本函数不发送任何网络通知。
    """

    if (
        isinstance(cooldown_seconds, bool)
        or not isinstance(cooldown_seconds, int)
        or cooldown_seconds < 1
    ):
        raise ValueError("cooldown_seconds 必须为正整数")

    init_admin_alert_db()

    issues = scan_task_health()
    now = utc_now()
    now_text = now.isoformat()

    queued_count = 0

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row

        with conn:
            conn.execute("BEGIN IMMEDIATE")

            for issue in issues:
                identity = get_issue_identity(issue)

                if identity is None:
                    continue

                (
                    issue_key,
                    issue_type,
                    task_id,
                    run_id,
                ) = identity

                state = conn.execute(
                    """
                    SELECT last_enqueued_at
                    FROM admin_alert_state
                    WHERE issue_key = ?
                    """,
                    (issue_key,)
                ).fetchone()

                if state is None:
                    conn.execute(
                        """
                        INSERT INTO admin_alert_state (
                            issue_key,
                            alert_type,
                            task_id,
                            run_id,
                            first_seen_at,
                            last_seen_at,
                            sightings
                        )
                        VALUES (?, ?, ?, ?, ?, ?, 1)
                        """,
                        (
                            issue_key,
                            issue_type,
                            task_id,
                            run_id,
                            now_text,
                            now_text,
                        )
                    )

                    last_enqueued_at = None

                else:
                    conn.execute(
                        """
                        UPDATE admin_alert_state
                        SET last_seen_at = ?,
                            sightings = sightings + 1
                        WHERE issue_key = ?
                        """,
                        (now_text, issue_key)
                    )

                    last_enqueued_at = state[
                        "last_enqueued_at"
                    ]

                # 已有未发送告警，不允许重复排队
                pending = conn.execute(
                    """
                    SELECT id
                    FROM admin_alert_outbox
                    WHERE issue_key = ?
                      AND status = 'pending'
                    LIMIT 1
                    """,
                    (issue_key,)
                ).fetchone()

                if pending is not None:
                    continue

                # 判断是否仍处于冷却时间
                if last_enqueued_at:
                    previous = datetime.fromisoformat(
                        last_enqueued_at
                    )

                    if previous.tzinfo is None:
                        previous = previous.replace(
                            tzinfo=timezone.utc
                        )

                    elapsed = (
                        now - previous.astimezone(timezone.utc)
                    ).total_seconds()

                    if elapsed < cooldown_seconds:
                        continue

                message = build_alert_message(
                    issue_type,
                    task_id
                )

                conn.execute(
                    """
                    INSERT INTO admin_alert_outbox (
                        issue_key,
                        message,
                        status,
                        created_at
                    )
                    VALUES (?, ?, 'pending', ?)
                    """,
                    (
                        issue_key,
                        message,
                        now_text
                    )
                )

                conn.execute(
                    """
                    UPDATE admin_alert_state
                    SET last_enqueued_at = ?
                    WHERE issue_key = ?
                    """,
                    (now_text, issue_key)
                )

                queued_count += 1

    if queued_count:
        logger.warning(
            "新增管理员待处理告警：%s 条",
            queued_count
        )

    return queued_count


def list_pending_admin_alerts(
    limit: int = 50,
) -> list[dict]:
    """查看等待管理员通知 Worker 处理的告警。"""

    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 200
    ):
        raise ValueError("limit 必须在 1 到 200 之间")

    init_admin_alert_db()

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute(
            """
            SELECT
                id,
                issue_key,
                message,
                status,
                created_at
            FROM admin_alert_outbox
            WHERE status = 'pending'
            ORDER BY created_at, id
            LIMIT ?
            """,
            (limit,)
        ).fetchall()

        return [dict(row) for row in rows]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    count = enqueue_health_alerts()

    print(f"新增管理员告警：{count} 条")
    print(
        "等待处理的告警：",
        len(list_pending_admin_alerts())
    )