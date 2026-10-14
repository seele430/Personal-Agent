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
STATUSES = {"pending", "sent", "cancelled"}
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,128}")


class ReminderConflict(Exception):
    """同一聊天请求对应了不同用户或提醒内容。"""


class ReminderNotFound(Exception):
    """提醒不存在，或不属于当前用户。

    两种情况返回同一个异常，避免泄露"该 ID 属于别人"。
    """


class ReminderNotEditable(Exception):
    """提醒已经发送或已取消，不能再修改或取消。"""


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

        供前端提醒面板轮询；不会发送 ntfy。
        前端展示后应调用 mark_delivered() 确认，
        否则同一条提醒会被反复返回。
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

    # ----------------------------------------------------------------
    # 查询、取消、修改、确认送达
    # ----------------------------------------------------------------

    @staticmethod
    def _clean_user_id(user_id: str) -> str:
        """校验并归一化用户 ID。"""

        if (
            not isinstance(user_id, str)
            or not user_id.strip()
            or len(user_id) > 128
        ):
            raise ValueError("无效的用户 ID")

        return user_id.strip()

    def list_for_user(
        self,
        *,
        user_id: str,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """列出指定用户自己的提醒。

        只按服务端认证后的 user_id 过滤，
        不接受调用方传入的其它用户身份。
        """

        user_id = self._clean_user_id(user_id)

        if status is not None and status not in STATUSES:
            raise ValueError("无效的提醒状态")

        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("无效的条数上限")

        self.init_db()

        sql = """
            SELECT id, message, minutes, remind_at,
                   delivery, status
            FROM reminder_requests
            WHERE user_id = ?
        """

        parameters: list = [user_id]

        if status is not None:
            sql += " AND status = ?"
            parameters.append(status)

        # 待发提醒排在最前，其余按新到旧。
        sql += """
            ORDER BY
                CASE WHEN status = 'pending' THEN 0 ELSE 1 END,
                remind_at,
                id DESC
            LIMIT ?
        """

        parameters.append(limit)

        with closing(self._connect()) as conn:
            rows = conn.execute(sql, parameters).fetchall()

        return [dict(row) for row in rows]

    def cancel(
        self,
        *,
        user_id: str,
        reminder_id: int,
    ) -> dict:
        """取消自己的一条待发提醒（软删除）。

        站长提醒同时存在于旧表 reminders 中，
        而 ntfy 调度器只认旧表的 status，
        所以必须把旧表记录一起置为 cancelled，
        否则取消之后仍然会推送通知。
        """

        user_id = self._clean_user_id(user_id)

        if type(reminder_id) is not int or reminder_id <= 0:
            raise ValueError("无效的提醒 ID")

        self.init_db()

        with closing(self._connect()) as conn:

            try:
                conn.execute("BEGIN IMMEDIATE")

                row = conn.execute(
                    """
                    SELECT id, status, legacy_reminder_id
                    FROM reminder_requests
                    WHERE id = ? AND user_id = ?
                    """,
                    (reminder_id, user_id),
                ).fetchone()

                if row is None:
                    raise ReminderNotFound(
                        "提醒不存在或无权访问"
                    )

                if row["status"] != "pending":
                    conn.commit()

                    return {
                        "id": reminder_id,
                        "status": row["status"],
                        "changed": False,
                    }

                if row["legacy_reminder_id"] is not None:
                    conn.execute(
                        """
                        UPDATE reminders
                        SET status = 'cancelled'
                        WHERE id = ? AND status = 'pending'
                        """,
                        (row["legacy_reminder_id"],),
                    )

                conn.execute(
                    """
                    UPDATE reminder_requests
                    SET status = 'cancelled'
                    WHERE id = ? AND user_id = ?
                      AND status = 'pending'
                    """,
                    (reminder_id, user_id),
                )

                conn.commit()

                return {
                    "id": reminder_id,
                    "status": "cancelled",
                    "changed": True,
                }

            except Exception:
                conn.rollback()
                raise

    def update(
        self,
        *,
        user_id: str,
        reminder_id: int,
        message: str | None = None,
        minutes: int | None = None,
        now: datetime | None = None,
    ) -> dict:
        """修改自己的一条待发提醒。

        只允许修改 pending 记录：
        已发送或已取消的提醒不能被改回待发，
        否则会重新触发一次通知。
        """

        user_id = self._clean_user_id(user_id)

        if type(reminder_id) is not int or reminder_id <= 0:
            raise ValueError("无效的提醒 ID")

        if message is None and minutes is None:
            raise ValueError("没有需要修改的内容")

        new_message = None

        if message is not None:

            if not isinstance(message, str):
                raise ValueError("提醒内容必须是文本")

            new_message = message.strip()

            if (
                not new_message
                or len(new_message.encode("utf-8")) > 1024
            ):
                raise ValueError("提醒内容为空或过长")

        if minutes is not None and (
            type(minutes) is not int
            or not 1 <= minutes <= 43200
        ):
            raise ValueError("提醒分钟数无效")

        self.init_db()

        with closing(self._connect()) as conn:

            try:
                conn.execute("BEGIN IMMEDIATE")

                row = conn.execute(
                    """
                    SELECT id, message, minutes, remind_at,
                           status, legacy_reminder_id
                    FROM reminder_requests
                    WHERE id = ? AND user_id = ?
                    """,
                    (reminder_id, user_id),
                ).fetchone()

                if row is None:
                    raise ReminderNotFound(
                        "提醒不存在或无权访问"
                    )

                if row["status"] != "pending":
                    raise ReminderNotEditable(
                        "提醒已发送或已取消，不能再修改"
                    )

                final_message = (
                    row["message"]
                    if new_message is None
                    else new_message
                )

                final_minutes = row["minutes"]

                if minutes is None:
                    remind_at = row["remind_at"]

                else:
                    final_minutes = minutes

                    remind_at = (
                        _check_time(now)
                        + timedelta(minutes=minutes)
                    ).isoformat()

                conn.execute(
                    """
                    UPDATE reminder_requests
                    SET message = ?, minutes = ?, remind_at = ?
                    WHERE id = ? AND user_id = ?
                      AND status = 'pending'
                    """,
                    (
                        final_message,
                        final_minutes,
                        remind_at,
                        reminder_id,
                        user_id,
                    ),
                )

                # 站长提醒的旧表记录必须同步，
                # 否则 ntfy 会按旧内容、旧时间推送。
                if row["legacy_reminder_id"] is not None:
                    conn.execute(
                        """
                        UPDATE reminders
                        SET message = ?, remind_at = ?
                        WHERE id = ? AND status = 'pending'
                        """,
                        (
                            final_message,
                            remind_at,
                            row["legacy_reminder_id"],
                        ),
                    )

                conn.commit()

                return {
                    "id": reminder_id,
                    "message": final_message,
                    "minutes": final_minutes,
                    "remind_at": remind_at,
                    "status": "pending",
                    "changed": True,
                }

            except Exception:
                conn.rollback()
                raise

    def mark_delivered(
        self,
        *,
        user_id: str,
        reminder_id: int,
        now: datetime | None = None,
    ) -> dict:
        """把已到期的站内提醒标记为已送达。

        前端确认展示过之后调用，
        否则 due_in_app() 会反复返回同一条提醒。
        尚未到期的提醒不能被提前确认。
        """

        user_id = self._clean_user_id(user_id)

        if type(reminder_id) is not int or reminder_id <= 0:
            raise ValueError("无效的提醒 ID")

        current = _check_time(now).isoformat()

        self.init_db()

        with closing(self._connect()) as conn:

            try:
                conn.execute("BEGIN IMMEDIATE")

                row = conn.execute(
                    """
                    SELECT id, status, delivery, remind_at
                    FROM reminder_requests
                    WHERE id = ? AND user_id = ?
                    """,
                    (reminder_id, user_id),
                ).fetchone()

                if row is None:
                    raise ReminderNotFound(
                        "提醒不存在或无权访问"
                    )

                if (
                    row["status"] != "pending"
                    or row["delivery"] != "in_app"
                ):
                    conn.commit()

                    return {
                        "id": reminder_id,
                        "status": row["status"],
                        "changed": False,
                    }

                if row["remind_at"] > current:
                    conn.commit()

                    return {
                        "id": reminder_id,
                        "status": row["status"],
                        "changed": False,
                    }

                conn.execute(
                    """
                    UPDATE reminder_requests
                    SET status = 'sent'
                    WHERE id = ? AND user_id = ?
                      AND status = 'pending'
                    """,
                    (reminder_id, user_id),
                )

                conn.commit()

                return {
                    "id": reminder_id,
                    "status": "sent",
                    "changed": True,
                }

            except Exception:
                conn.rollback()
                raise