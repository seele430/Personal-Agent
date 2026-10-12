
"""聊天准入：在同一个 SQLite 事务里检查次数、并发和预算。

当前只提供数据层。正式 API 尚未切换到此函数。
初始预留 US$0.05 / 新请求，全站滚动 24 小时 US$1。
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.core.ai_budget import BudgetExceeded
from src.core.ai_pricing import validate_provider
from src.core.chat_ownership import (
    RequestMessageConflict,
    RequestOwnerMismatch,
)
from src.core.chat_quota import (
    DAILY_REQUEST_LIMITS,
    GLOBAL_CONCURRENT_LIMIT,
    USER_CONCURRENT_LIMIT,
    ChatConcurrencyLimitExceeded,
    ChatDailyLimitExceeded,
)

GLOBAL_BUDGET_USD = Decimal("1.00")
RESERVE_PER_REQUEST_USD = Decimal("0.05")

# ai_budget.py 使用十亿分之一美元。
GLOBAL_BUDGET_UNITS = 1_000_000_000
RESERVE_UNITS = 50_000_000


def admit_chat_request(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    digest: str,
    user_id: str,
    role: str,
    provider_host: str,
    model: str,
    now: datetime | None = None,
) -> dict:
    """原子准入新聊天请求。

    返回：
        admitted=True  新请求已获得预算预留
        admitted=False 原请求已存在，不能重复调用模型

    要求：
        1. 使用本次操作专用的 SQLite 连接
        2. 已创建 chat_requests 表及 user_id 字段
        3. 已执行 init_budget_schema(conn)
        4. 不能在已有事务中调用
    """

    for field, value in (
        ("request_id", request_id),
        ("digest", digest),
        ("user_id", user_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} 不能为空")

    if role not in DAILY_REQUEST_LIMITS:
        raise ValueError("未知用户角色")

    # 未核实价格的服务商或模型不允许新调用。
    validate_provider(provider_host, model)

    current = (
        datetime.now(timezone.utc)
        if now is None
        else now
    )

    if (
        not isinstance(current, datetime)
        or current.tzinfo is None
        or current.utcoffset() is None
    ):
        raise ValueError("时间必须带时区")

    current = current.astimezone(timezone.utc)
    timestamp = current.isoformat()
    cutoff = (
        current - timedelta(hours=24)
    ).isoformat()

    # 所有检查和两张表的写入在一个事务内。
    with conn:
        conn.execute("BEGIN IMMEDIATE")

        existing = conn.execute(
            """
            SELECT
                message_hash,
                status,
                reply,
                user_id
            FROM chat_requests
            WHERE request_id = ?
            """,
            (request_id,),
        ).fetchone()

        # 优先检查已有请求，防止网络重试重复占预算。
        if existing is not None:
            if existing[3] != user_id:
                raise RequestOwnerMismatch(
                    "请求不存在或无权访问"
                )

            if existing[0] != digest:
                raise RequestMessageConflict(
                    "同一 request_id 不能对应不同消息"
                )

            return {
                "admitted": False,
                "request_id": request_id,
                "status": existing[1],
                "reply": existing[2],
            }

        daily_used = conn.execute(
            """
            SELECT COUNT(*)
            FROM chat_requests
            WHERE user_id = ?
              AND created_at > ?
            """,
            (user_id, cutoff),
        ).fetchone()[0]

        if daily_used >= DAILY_REQUEST_LIMITS[role]:
            raise ChatDailyLimitExceeded(
                "过去24小时请求次数已用完"
            )

        user_active = conn.execute(
            """
            SELECT COUNT(*)
            FROM chat_requests
            WHERE user_id = ?
              AND status = 'processing'
            """,
            (user_id,),
        ).fetchone()[0]

        if user_active >= USER_CONCURRENT_LIMIT:
            raise ChatConcurrencyLimitExceeded(
                "用户并发额度已满"
            )

        global_active = conn.execute(
            """
            SELECT COUNT(*)
            FROM chat_requests
            WHERE status = 'processing'
            """
        ).fetchone()[0]

        if global_active >= GLOBAL_CONCURRENT_LIMIT:
            raise ChatConcurrencyLimitExceeded(
                "全站并发额度已满"
            )

        # 已结算请求按实际记录金额计入预算；
        # 未结算、用量未知请求按预留额度计入预算。
        budget_used = conn.execute(
            """
            SELECT COALESCE(
                SUM(
                    COALESCE(
                        charged_nano_usd,
                        reserved_nano_usd
                    )
                ),
                0
            )
            FROM ai_budget_reservations
            WHERE created_at > ?
            """,
            (cutoff,),
        ).fetchone()[0]

        if (
            budget_used + RESERVE_UNITS
            > GLOBAL_BUDGET_UNITS
        ):
            raise BudgetExceeded(
                "全站过去24小时预算不足"
            )

        # 两张表的插入要么都成功，要么都回滚。
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
            VALUES (?, ?, 'processing', NULL, ?, ?, ?)
            """,
            (
                request_id,
                digest,
                timestamp,
                timestamp,
                user_id,
            ),
        )

        conn.execute(
            """
            INSERT INTO ai_budget_reservations (
                request_id,
                user_id,
                reserved_nano_usd,
                charged_nano_usd,
                state,
                created_at
            )
            VALUES (?, ?, ?, NULL, 'reserved', ?)
            """,
            (
                request_id,
                user_id,
                RESERVE_UNITS,
                timestamp,
            ),
        )

    return {
        "admitted": True,
        "request_id": request_id,
        "status": "processing",
        "reserved_usd": str(
            RESERVE_PER_REQUEST_USD
        ),
    }