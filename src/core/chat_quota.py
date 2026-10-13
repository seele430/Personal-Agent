"""原子限制每用户新聊天次数与并发；只提供数据层，不直接更改 API。"""

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Mapping

from src.core.chat_ownership import (
    RequestMessageConflict,
    RequestOwnerMismatch,
)

# 过去 24 小时内每个角色可发起的新请求次数。
# member 按 DEVELOPMENT_PLAN.md 定为每天 10 次。
DAILY_REQUEST_LIMITS = {
    "member": 10,
    "trusted": 40,
    "admin": 80,
    "owner": 100,
}

USER_CONCURRENT_LIMIT = 2
GLOBAL_CONCURRENT_LIMIT = 4


class ChatDailyLimitExceeded(Exception):
    """过去 24 小时的新请求次数已用完。"""


class ChatConcurrencyLimitExceeded(Exception):
    """用户或服务器正在处理的请求过多。"""


def prepare_chat_quota(conn: sqlite3.Connection) -> None:
    """在已有用户归属列上建索引，安全且可重复执行。"""

    columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(chat_requests)"
        )
    }

    if "user_id" not in columns:
        raise sqlite3.OperationalError(
            "请先执行 migrate_chat_ownership"
        )

    conn.execute(
        "CREATE INDEX IF NOT EXISTS "
        "idx_chat_quota_user_created "
        "ON chat_requests(user_id, created_at)"
    )


def claim_limited_request(
    conn: sqlite3.Connection,
    request_id: str,
    digest: str,
    user_id: str,
    role: str,
    *,
    now: datetime | None = None,
    daily_limits: Mapping[str, int] | None = None,
    user_concurrent_limit: int = USER_CONCURRENT_LIMIT,
    global_concurrent_limit: int = GLOBAL_CONCURRENT_LIMIT,
) -> dict | None:
    """原子领取请求。

    返回 None：成功领取新请求，可以调用模型。
    返回 dict：当前用户已有相同请求，必须复用原结果。

    超额时抛出异常，不插入请求记录。

    要求使用本次操作专用、尚未开启事务的 SQLite 连接。
    """

    if (
        not isinstance(user_id, str)
        or not user_id.strip()
    ):
        raise ValueError("无效的服务端用户 ID")

    if not isinstance(request_id, str) or not request_id:
        raise ValueError("request_id 不能为空")

    if not isinstance(digest, str) or not digest:
        raise ValueError("消息摘要不能为空")

    limits = (
        daily_limits
        if daily_limits is not None
        else DAILY_REQUEST_LIMITS
    )

    if role not in limits:
        raise ValueError("未知角色")

    daily_limit = limits[role]

    for limit in (
        daily_limit,
        user_concurrent_limit,
        global_concurrent_limit,
    ):
        if type(limit) is not int or limit < 1:
            raise ValueError("额度必须是正整数")

    current = (
        datetime.now(timezone.utc)
        if now is None
        else now
    )

    if (
        current.tzinfo is None
        or current.utcoffset() is None
    ):
        raise ValueError("时间必须包含时区")

    current = current.astimezone(timezone.utc)

    timestamp = current.isoformat()

    cutoff = (
        current - timedelta(hours=24)
    ).isoformat()

    # 用 SQLite 写事务确保：
    # 检查额度和领取请求不可被其他请求穿插。
    with conn:
        conn.execute("BEGIN IMMEDIATE")

        existing = conn.execute(
            """
            SELECT
                request_id,
                message_hash,
                status,
                reply,
                user_id
            FROM chat_requests
            WHERE request_id = ?
            """,
            (request_id,),
        ).fetchone()

        # 已有请求优先检查所有权和消息摘要。
        # 不重复占用次数或并发额度。
        if existing is not None:

            if existing[4] != user_id:
                raise RequestOwnerMismatch(
                    "请求不存在或无权访问"
                )

            if existing[1] != digest:
                raise RequestMessageConflict(
                    "同一 request_id 不能对应不同消息"
                )

            return {
                "request_id": existing[0],
                "message_hash": existing[1],
                "status": existing[2],
                "reply": existing[3],
            }

        # completed、processing、unknown 均计入次数。
        # 模型结果不明时不能通过重复请求刷新额度。
        daily_used = conn.execute(
            """
            SELECT COUNT(*)
            FROM chat_requests
            WHERE user_id = ?
              AND created_at > ?
            """,
            (user_id, cutoff),
        ).fetchone()[0]

        if daily_used >= daily_limit:
            raise ChatDailyLimitExceeded(
                "过去 24 小时聊天次数已达到上限"
            )

        # 检查当前用户的在途请求。
        user_active = conn.execute(
            """
            SELECT COUNT(*)
            FROM chat_requests
            WHERE user_id = ?
              AND status = 'processing'
            """,
            (user_id,),
        ).fetchone()[0]

        if user_active >= user_concurrent_limit:
            raise ChatConcurrencyLimitExceeded(
                "该用户正在处理的请求过多"
            )

        # 检查整个服务的在途请求。
        global_active = conn.execute(
            """
            SELECT COUNT(*)
            FROM chat_requests
            WHERE status = 'processing'
            """
        ).fetchone()[0]

        if global_active >= global_concurrent_limit:
            raise ChatConcurrencyLimitExceeded(
                "系统忙，请稍后再试"
            )

        # 只有全部检查通过后才领取新请求。
        conn.execute(
            """
            INSERT INTO chat_requests (
                request_id,
                message_hash,
                status,
                reply,
                created_at,
                updated_at,
                user_id
            )
            VALUES (
                ?, ?, 'processing', NULL, ?, ?, ?
            )
            """,
            (
                request_id,
                digest,
                timestamp,
                timestamp,
                user_id,
            ),
        )

    return None