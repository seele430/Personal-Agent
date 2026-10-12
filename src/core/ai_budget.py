
"""Conservative budget reservation ledger.

只提供预算数据层，暂不接入正式聊天 API。
所有金额均为美元，内部使用整数 nano-USD。
未知调用结果不会被当成免费。
"""

from __future__ import annotations

import sqlite3

from datetime import datetime, timedelta, timezone
from decimal import (
    Decimal,
    InvalidOperation,
    ROUND_CEILING,
)


UNITS = Decimal("1000000000")


class BudgetExceeded(Exception):
    """预算不足，禁止启动新调用。"""


class BudgetConflict(Exception):
    """请求 ID 已存在或结算数据冲突。"""


def _money_units(
    value,
    *,
    allow_zero=False,
) -> int:
    """将美元金额向上取整为 nano-USD。"""

    if (
        isinstance(value, bool)
        or not isinstance(
            value,
            (str, int, Decimal),
        )
    ):
        raise ValueError(
            "金额必须是字符串、整数或 Decimal"
        )

    try:
        amount = Decimal(value)

    except (InvalidOperation, ValueError):
        raise ValueError("无效的美元金额") from None

    if (
        not amount.is_finite()
        or amount < 0
        or (not allow_zero and amount == 0)
    ):
        raise ValueError(
            "金额必须为有效的正数"
        )

    result = int(
        (amount * UNITS).to_integral_value(
            rounding=ROUND_CEILING
        )
    )

    if result > 9_000_000_000_000_000_000:
        raise ValueError("金额超过数据库支持范围")

    return result


def _require_identity(
    value: str,
    field: str,
) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
    ):
        raise ValueError(
            f"{field} 不能为空"
        )

    return value


def _utc_time(
    now: datetime | None,
) -> datetime:

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
        raise ValueError(
            "时间必须包含时区"
        )

    return current.astimezone(timezone.utc)


# ==========================================
# 创建预算账本
# ==========================================

def init_budget_schema(
    conn: sqlite3.Connection,
) -> None:
    """创建独立表，不修改现有聊天记录。"""

    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS
            ai_budget_reservations (

                request_id TEXT PRIMARY KEY,

                user_id TEXT NOT NULL,

                reserved_nano_usd INTEGER NOT NULL
                    CHECK(reserved_nano_usd > 0),

                charged_nano_usd INTEGER
                    CHECK(charged_nano_usd >= 0),

                state TEXT NOT NULL
                    CHECK(state IN (
                        'reserved',
                        'settled',
                        'uncertain',
                        'overrun'
                    )),

                created_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_budget_user_created

            ON ai_budget_reservations(
                user_id,
                created_at
            )
            """
        )


# ==========================================
# 原子预留预算
# ==========================================

def reserve_budget(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    user_id: str,
    reserve_usd: str | Decimal,
    user_limit_usd: str | Decimal,
    global_limit_usd: str | Decimal,
    now: datetime | None = None,
) -> dict:
    """原子预留新请求的预计最高费用。

    返回 admitted=True：成功预留新请求。
    返回 admitted=False：原请求已经预留。

    预算不足时不会写入新记录。

    要求本操作使用独立且尚未开启事务的连接。
    """

    _require_identity(
        request_id,
        "request_id",
    )

    _require_identity(
        user_id,
        "user_id",
    )

    amount = _money_units(
        reserve_usd
    )

    user_cap = _money_units(
        user_limit_usd
    )

    global_cap = _money_units(
        global_limit_usd
    )

    current = _utc_time(now)

    timestamp = current.isoformat()

    cutoff = (
        current - timedelta(hours=24)
    ).isoformat()

    # BEGIN IMMEDIATE：
    # 将检查额度和预留记录放在同一写事务中。
    with conn:

        conn.execute("BEGIN IMMEDIATE")

        existing = conn.execute(
            """
            SELECT
                user_id,
                reserved_nano_usd,
                charged_nano_usd,
                state

            FROM ai_budget_reservations

            WHERE request_id = ?
            """,
            (request_id,),
        ).fetchone()

        # 旧请求不能再次占用预算
        if existing is not None:

            if (
                existing[0] != user_id
                or existing[1] != amount
            ):
                raise BudgetConflict(
                    "请求 ID 已存在或无权使用"
                )

            return {
                "admitted": False,
                "state": existing[3],
                "reserved_nano_usd": existing[1],
                "charged_nano_usd": existing[2],
            }

        # 当前用户过去 24 小时已占用金额
        user_used = conn.execute(
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

            WHERE user_id = ?
              AND created_at > ?
            """,
            (
                user_id,
                cutoff,
            ),
        ).fetchone()[0]

        # 全体用户过去 24 小时已占用金额
        global_used = conn.execute(
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
            user_used + amount > user_cap
            or global_used + amount > global_cap
        ):
            raise BudgetExceeded(
                "个人或全站预算不足"
            )

        # 全部检查通过，才建立预算预留
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
                amount,
                timestamp,
            ),
        )

    return {
        "admitted": True,
        "state": "reserved",
        "reserved_nano_usd": amount,
        "charged_nano_usd": None,
    }


# ==========================================
# 模型全部调用结束后结算
# ==========================================

def settle_budget(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    user_id: str,
    actual_usd: str | Decimal | None,
) -> str:
    """结算整个聊天请求的所有模型调用。

    actual_usd=None：
        用量未知，保留整个预留金额。

    actual_usd <= 预留：
        以实际费用结算，释放差额。

    actual_usd > 预留：
        记录超支，并返回 overrun。

    如果一条聊天进行了两次模型调用，
    必须等待两次用量都已确认后才能结算。
    """

    _require_identity(
        request_id,
        "request_id",
    )

    _require_identity(
        user_id,
        "user_id",
    )

    actual = (
        None
        if actual_usd is None
        else _money_units(
            actual_usd,
            allow_zero=True,
        )
    )

    with conn:

        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            """
            SELECT
                user_id,
                reserved_nano_usd,
                charged_nano_usd,
                state

            FROM ai_budget_reservations

            WHERE request_id = ?
            """,
            (request_id,),
        ).fetchone()

        if (
            row is None
            or row[0] != user_id
        ):
            raise BudgetConflict(
                "请求不存在或无权结算"
            )

        # 已结算请求不能被改成另一个费用
        if row[3] != "reserved":

            expected = (
                row[1]
                if actual is None
                else actual
            )

            if row[2] != expected:
                raise BudgetConflict(
                    "该请求已有不同的结算结果"
                )

            return row[3]

        if actual is None:
            state = "uncertain"
            charged = row[1]

        elif actual > row[1]:
            state = "overrun"
            charged = actual

        else:
            state = "settled"
            charged = actual

        conn.execute(
            """
            UPDATE ai_budget_reservations

            SET
                state = ?,
                charged_nano_usd = ?

            WHERE
                request_id = ?
                AND user_id = ?
            """,
            (
                state,
                charged,
                request_id,
                user_id,
            ),
        )

    return state


# ==========================================
# 查询本地预算使用情况
# ==========================================

def budget_usage(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    now: datetime | None = None,
) -> dict:
    """查询过去 24 小时预算占用情况。

    这是本地账本，不等于服务商实际账单。
    """

    _require_identity(
        user_id,
        "user_id",
    )

    cutoff = (
        _utc_time(now)
        - timedelta(hours=24)
    ).isoformat()

    row = conn.execute(
        """
        SELECT
            COUNT(*),

            COALESCE(
                SUM(
                    COALESCE(
                        charged_nano_usd,
                        reserved_nano_usd
                    )
                ),
                0
            ),

            SUM(
                CASE
                    WHEN state IN (
                        'reserved',
                        'uncertain',
                        'overrun'
                    )
                    THEN 1
                    ELSE 0
                END
            )

        FROM ai_budget_reservations

        WHERE
            user_id = ?
            AND created_at > ?
        """,
        (
            user_id,
            cutoff,
        ),
    ).fetchone()

    return {
        "requests": row[0],

        "accounted_usd": str(
            Decimal(row[1]) / UNITS
        ),

        "unresolved_or_overrun": (
            row[2] or 0
        ),
    }