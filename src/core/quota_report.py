"""个人额度只读汇总。

数字必须来自"强制执行额度时用的同一批表和同一个 24 小时窗口"，
否则面板显示的额度和真实拦截行为会不一致：
用户看到还有余额，请求却被拒。
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.core.ai_budget import UNITS, budget_usage
from src.core.chat_admission import (
    USER_BUDGET_LIMIT_UNITS,
)
from src.core.chat_quota import DAILY_REQUEST_LIMITS


WINDOW_HOURS = 24

# 本地账本，不等于服务商账单。
LEDGER_NOTE = (
    "本地账本：只统计本机记录，不等于服务商账单。"
)


def _money(value: Decimal) -> str:
    """固定用普通小数显示，避免出现 1E-9 这种科学计数法。"""

    return format(value, "f")


def build_quota_report(
    conn,
    *,
    user_id: str,
    role: str,
    now: datetime | None = None,
) -> dict:
    """汇总某个用户过去 24 小时的额度占用。

    conn 必须是聊天请求库：
    chat_requests 与 ai_budget_reservations 都在这里。
    """

    if role not in DAILY_REQUEST_LIMITS:
        raise ValueError("未知用户角色")

    if role not in USER_BUDGET_LIMIT_UNITS:
        raise ValueError("未知用户角色")

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
        raise ValueError("时间必须包含时区")

    current = current.astimezone(timezone.utc)

    cutoff = (
        current - timedelta(hours=WINDOW_HOURS)
    ).isoformat()

    # 与 admit_chat_request 的计数口径完全一致。
    used_requests = conn.execute(
        """
        SELECT COUNT(*)
        FROM chat_requests
        WHERE user_id = ?
          AND created_at > ?
        """,
        (user_id, cutoff),
    ).fetchone()[0]

    usage = budget_usage(
        conn,
        user_id=user_id,
        now=current,
    )

    request_limit = DAILY_REQUEST_LIMITS[role]

    budget_limit = (
        Decimal(USER_BUDGET_LIMIT_UNITS[role]) / UNITS
    )

    used_usd = Decimal(usage["accounted_usd"])

    remaining_usd = budget_limit - used_usd

    if remaining_usd < 0:
        remaining_usd = Decimal("0")

    return {
        "window_hours": WINDOW_HOURS,
        "role": role,
        "requests": {
            "used": used_requests,
            "limit": request_limit,
            "remaining": max(
                request_limit - used_requests,
                0,
            ),
        },
        "budget": {
            "used_usd": _money(used_usd),
            "limit_usd": _money(budget_limit),
            "remaining_usd": _money(remaining_usd),
            # 还没结算完的预留：
            # 它会让"已用"继续变化，所以单独说明。
            "unresolved_reservations": (
                usage["unresolved_or_overrun"]
            ),
        },
        "note": LEDGER_NOTE,
    }
