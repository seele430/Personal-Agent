"""按主机的抓取频率限制。

为什么放在调度层而不是抓取层：
`fetch_rss` 只负责"安全地做一次请求"，而"同一台主机多久允许被
抓一次"是**调度策略**。把它放进抓取函数会让每次手工调用都被限流，
也让测试必须绕过它。

同一个 SQLite 库（tasks.db）里记每台主机最后一次真正发起抓取的时间，
`BEGIN IMMEDIATE` 保证并发调度下不会同时放行。

时间通过 `now=` 注入，测试不需要真的等待。
"""

import sqlite3

from contextlib import closing
from datetime import datetime, timedelta, timezone

from src.config import paths


# 同一台主机的最小抓取间隔。
MIN_HOST_INTERVAL_SECONDS = 300

TABLE = "feed_fetch_log"


def utc_time(value: datetime | None = None) -> datetime:
    """归一化成带时区的 UTC 时间。"""

    current = (
        datetime.now(timezone.utc)
        if value is None
        else value
    )

    if (
        not isinstance(current, datetime)
        or current.tzinfo is None
        or current.utcoffset() is None
    ):
        raise ValueError("时间必须包含时区")

    return current.astimezone(timezone.utc)


def init_rate_limit_db() -> None:
    """创建抓取记录表（可重复执行）。"""

    with closing(
        sqlite3.connect(paths.tasks_db, timeout=10)
    ) as conn, conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                host TEXT PRIMARY KEY,
                last_started_at TEXT NOT NULL
            )
            """
        )


def current_host_state(
    host: str,
) -> datetime | None:
    """读取某主机最后一次抓取时间，没有则返回 None。"""

    init_rate_limit_db()

    with closing(
        sqlite3.connect(paths.tasks_db, timeout=10)
    ) as conn:
        row = conn.execute(
            f"""
            SELECT last_started_at
            FROM {TABLE}
            WHERE host = ?
            """,
            (host,),
        ).fetchone()

    if row is None:
        return None

    return datetime.fromisoformat(row[0])


def reserve_fetch(
    host: str,
    *,
    now: datetime | None = None,
    interval_seconds: int = MIN_HOST_INTERVAL_SECONDS,
) -> int:
    """尝试占用某主机的一次抓取额度。

    返回 0：放行，并已记录本次抓取时间；
    返回正整数：被限流，值是建议等待的秒数（不记录）。
    """

    if not isinstance(host, str) or not host.strip():
        raise ValueError("host 不能为空")

    if (
        isinstance(interval_seconds, bool)
        or not isinstance(interval_seconds, int)
        or interval_seconds < 1
    ):
        raise ValueError("间隔必须是正整数秒")

    host = host.strip().lower()

    current = utc_time(now)

    init_rate_limit_db()

    with closing(
        sqlite3.connect(paths.tasks_db, timeout=10)
    ) as conn:

        try:
            conn.execute("BEGIN IMMEDIATE")

            row = conn.execute(
                f"""
                SELECT last_started_at
                FROM {TABLE}
                WHERE host = ?
                """,
                (host,),
            ).fetchone()

            if row is not None:
                last = datetime.fromisoformat(row[0])

                elapsed = (
                    current - last
                ).total_seconds()

                if elapsed < interval_seconds:
                    conn.commit()

                    return max(
                        int(interval_seconds - elapsed),
                        1,
                    )

            conn.execute(
                f"""
                INSERT INTO {TABLE} (
                    host,
                    last_started_at
                )
                VALUES (?, ?)

                ON CONFLICT(host) DO UPDATE SET
                    last_started_at =
                        excluded.last_started_at
                """,
                (host, current.isoformat()),
            )

            conn.commit()

            return 0

        except Exception:
            conn.rollback()
            raise


def retry_at(
    host: str,
    *,
    now: datetime | None = None,
    interval_seconds: int = MIN_HOST_INTERVAL_SECONDS,
) -> str:
    """该主机下一次**允许**被抓取的时间（ISO 字符串）。

    从没抓过 → 就是现在；刚抓过 → 上次抓取时间 + 间隔。

    注意这里必须读 host 的真实状态。早先的实现直接返回
    "现在 + 间隔"，与 host 无关 —— 那样"立即检查"会告诉用户
    "要等 5 分钟"，而实际上这个主机可能从没抓过。
    """

    current = utc_time(now)
    last = current_host_state(host)

    if last is None:
        return current.isoformat()

    allowed = last + timedelta(seconds=interval_seconds)

    return max(current, allowed).isoformat()
