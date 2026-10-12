"""多用户提醒数据层。

兼容旧 reminders 表。
站长新提醒写入旧表，继续由 ntfy 调度器处理。
其他用户提醒只写入新表，不进入全站 ntfy。

调用方必须传入服务端认证后的 user_id 和 role。
"""

import re
import sqlite3

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROLES = {"owner", "admin", "trusted", "member"}
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,128}")


class ReminderConflict(Exception):
    """同一聊天请求对应了不同用户或提醒内容。"""


def _check_time(now):
    if now is None:
        return datetime.now(timezone.utc)

    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
    ):
        raise ValueError("时间必须包含时区")

    return now.astimezone(timezone.utc)


class ReminderStore:

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    def _connect(self):
        conn = sqlite3.connect(
            self.db_path,
            timeout=10,
        )

        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_db(self):
        """只创建缺失的表，不删除或改写历史提醒。"""

        self.db_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        with closing(self._connect()) as conn, conn:

            # 兼容原有四字段结构。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message TEXT NOT NULL,
                    remind_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                )
                """
            )

            # 新增的多用户提醒记录。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reminder_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    request_id TEXT NOT NULL UNIQUE,
                    user_id TEXT NOT NULL,

                    delivery TEXT NOT NULL
                        CHECK(delivery IN ('ntfy', 'in_app')),

                    message TEXT NOT NULL,
                    minutes INTEGER NOT NULL,
                    remind_at TEXT NOT NULL,

                    legacy_reminder_id INTEGER
                        REFERENCES reminders(id),

                    status TEXT NOT NULL DEFAULT 'pending',

                    CHECK (
                        (delivery = 'ntfy'
                         AND legacy_reminder_id IS NOT NULL)
                        OR
                        (delivery = 'in_app'
                         AND legacy_reminder_id IS NULL)
                    )
                )
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_reminder_requests_user_due
                ON reminder_requests (
                    user_id,
                    delivery,
                    status,
                    remind_at
                )
                """
            )

    def create(
        self,
        *,
        user_id: str,
        request_id: str,
        role: str,
        message: str,
        minutes: int,
        now: datetime | None = None,
    ) -> dict:
        """原子创建提醒，并按 request_id 防重复。

        返回 created=False 表示原请求已创建过，
        此时不会再次插入提醒。
        """

        if (
            not isinstance(user_id, str)
            or not user_id.strip()
            or len(user_id) > 128
        ):
            raise ValueError("无效的用户 ID")

        user_id = user_id.strip()

        if (
            not isinstance(request_id, str)
            or REQUEST_ID_PATTERN.fullmatch(request_id) is None
        ):
            raise ValueError("无效的 request_id")

        if role not in ROLES:
            raise ValueError("未知用户角色")

        if not isinstance(message, str):
            raise ValueError("提醒内容必须是文本")

        message = message.strip()

        if (
            not message
            or len(message.encode("utf-8")) > 1024
        ):
            raise ValueError("提醒内容为空或过长")

        if (
            type(minutes) is not int
            or not 1 <= minutes <= 43200
        ):
            raise ValueError("提醒分钟数无效")

        current = _check_time(now)
        remind_at = (
            current + timedelta(minutes=minutes)
        ).isoformat()

        # 只有站长使用全站 ntfy。
        delivery = (
            "ntfy" if role == "owner"
            else "in_app"
        )

        self.init_db()

        with closing(self._connect()) as conn:

            try:
                # 防止两个进程同时创建同一个请求。
                conn.execute("BEGIN IMMEDIATE")

                existing = conn.execute(
                    """
                    SELECT *
                    FROM reminder_requests
                    WHERE request_id = ?
                    """,
                    (request_id,),
                ).fetchone()

                if existing is not None:

                    if existing["user_id"] != user_id:
                        raise ReminderConflict(
                            "请求已存在或无权使用"
                        )

                    if (
                        existing["message"] != message
                        or existing["minutes"] != minutes
                        or existing["delivery"] != delivery
                    ):
                        raise ReminderConflict(
                            "同一请求不能修改提醒内容"
                        )

                    result = {
                        "id": existing["id"],
                        "legacy_reminder_id":
                            existing["legacy_reminder_id"],
                        "delivery": existing["delivery"],
                        "remind_at": existing["remind_at"],
                        "created": False,
                    }

                else:
                    legacy_id = None

                    if delivery == "ntfy":
                        # 站长提醒沿用旧表和原调度器。
                        cursor = conn.execute(
                            """
                            INSERT INTO reminders (
                                message,
                                remind_at,
                                status
                            )
                            VALUES (?, ?, 'pending')
                            """,
                            (message, remind_at),
                        )

                        legacy_id = cursor.lastrowid

                    # 普通用户不会向旧表写入记录。
                    cursor = conn.execute(
                        """
                        INSERT INTO reminder_requests (
                            request_id,
                            user_id,
                            delivery,
                            message,
                            minutes,
                            remind_at,
                            legacy_reminder_id,
                            status
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                        """,
                        (
                            request_id,
                            user_id,
                            delivery,
                            message,
                            minutes,
                            remind_at,
                            legacy_id,
                        ),
                    )

                    result = {
                        "id": cursor.lastrowid,
                        "legacy_reminder_id": legacy_id,
                        "delivery": delivery,
                        "remind_at": remind_at,
                        "created": True,
                    }

                conn.commit()
                return result

            except Exception:
                conn.rollback()
                raise

    def due_in_app(
        self,
        *,
        user_id: str,
        now: datetime | None = None,
    ) -> list[dict]:
        """只读取指定用户已到期的站内提醒。

        尚未接入前端，也不会发送 ntfy。
        """

        if (
            not isinstance(user_id, str)
            or not user_id.strip()
        ):
            raise ValueError("无效的用户 ID")

        current = _check_time(now).isoformat()

        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT id, message, remind_at
                FROM reminder_requests
                WHERE user_id = ?
                  AND delivery = 'in_app'
                  AND status = 'pending'
                  AND remind_at <= ?
                ORDER BY remind_at, id
                LIMIT 100
                """,
                (user_id.strip(), current),
            ).fetchall()

        return [dict(row) for row in rows]