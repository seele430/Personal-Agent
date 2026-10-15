
import logging
import math
import sqlite3

from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from uuid import uuid4

import requests

from src.config import paths
from src.core.monitor_registry import init_monitor_registry
from src.core.task_run_history import (
    init_run_history,
    start_task_run,
    finish_task_run,
)
from src.core.access_policy import (
    decide_access,
    AccessAction,
)
from src.monitors.rate_limit import (
    MIN_HOST_INTERVAL_SECONDS,
    reserve_fetch,
)
from src.core.source_registry import (
    DEFAULT_SOURCE_TYPE,
    run_source,
)
from src.monitors.url_policy import (
    UrlPolicyError,
    check_host_allowed,
    check_url_shape,
)


logger = logging.getLogger("personal_agent.dispatcher")

# 数据源是否获准执行，统一由 URL 策略判断：
# 协议 / 主机形态 / 字面量 IP（结构层），加上可选的域名白名单
# （环境变量 RSS_ALLOWED_HOSTS，不设置 = 除策略外不做额外限制）。
# 之前的硬编码单源白名单已由它取代，避免调度层与接口层两处判断漂移。

LEASE_SECONDS = 300
NETWORK_RETRY_SECONDS = 60
MAX_BACKOFF_SECONDS = 6 * 60 * 60


def utc_now():
    return datetime.now(timezone.utc)


# =====================================
# Database Initialization
# =====================================

def init_dispatcher_db() -> bool:
    """初始化租约、失败计数和任务历史。"""

    init_monitor_registry()

    with closing(
        sqlite3.connect(paths.tasks_db, timeout=10)
    ) as conn:
        with conn:
            columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(monitor_tasks)"
                )
            }

            migrations = {
                "lease_token": "TEXT",
                "lease_expires_at": "TEXT",
                "failure_count": (
                    "INTEGER NOT NULL DEFAULT 0"
                ),
            }

            for name, column_type in migrations.items():
                if name not in columns:
                    conn.execute(
                        f"ALTER TABLE monitor_tasks "
                        f"ADD COLUMN {name} {column_type}"
                    )

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_monitor_due
                ON monitor_tasks (
                    status,
                    next_run_at,
                    lease_expires_at
                )
            """)

    init_run_history()
    return True


# =====================================
# Retry-After / Backoff
# =====================================

def read_retry_after(response) -> int | None:
    """解析 Retry-After 秒数或 HTTP 日期。"""

    if response is None:
        return None

    value = response.headers.get("Retry-After")

    if not value:
        return None

    value = value.strip()

    try:
        seconds = int(value)
        return max(0, seconds)
    except ValueError:
        pass

    try:
        retry_date = parsedate_to_datetime(value)

        if retry_date.tzinfo is None:
            retry_date = retry_date.replace(
                tzinfo=timezone.utc
            )

        seconds = math.ceil(
            (retry_date - utc_now()).total_seconds()
        )

        return max(0, seconds)

    except (ValueError, TypeError, OverflowError):
        logger.warning("无法解析 Retry-After")
        return None


def calculate_retry_delay(
    task: dict,
    base_seconds: int,
    server_seconds: int | None = None
) -> int:
    """
    连续失败后指数退避。

    本地退避最多 6 小时；
    服务端 Retry-After 可以要求更久。
    """

    failures = max(
        0,
        int(task.get("failure_count") or 0)
    )

    exponent = min(failures, 16)

    delay = min(
        MAX_BACKOFF_SECONDS,
        max(1, base_seconds) * (2 ** exponent)
    )

    if server_seconds is not None:
        delay = max(delay, server_seconds)

    return max(1, delay)


# =====================================
# Classify Failure
# =====================================

def classify_failure(
    task: dict,
    exc: Exception
) -> tuple[bool, int | None, str]:
    """
    返回：
        pause_task
        retry_seconds
        error_message

    注意：
    AccessAction.PAUSE 表示暂缓请求，
    不一定代表数据库 status='paused'。
    """

    if isinstance(exc, requests.HTTPError):
        response = exc.response

        if response is not None:
            code = response.status_code
            decision = decide_access(code)

            message = (
                f"HTTP {code}: {decision.reason}"
            )

            if decision.action == AccessAction.STOP:
                return True, None, message

            if decision.action in (
                AccessAction.PAUSE,
                AccessAction.RETRY,
            ):
                delay = calculate_retry_delay(
                    task,
                    max(1, decision.retry_seconds),
                    read_retry_after(response)
                )

                return False, delay, message

            return (
                True,
                None,
                f"异常 HTTP 响应状态：{code}"
            )

    if isinstance(
        exc,
        (requests.RequestException, TimeoutError)
    ):
        delay = calculate_retry_delay(
            task,
            NETWORK_RETRY_SECONDS
        )

        message = (
            f"{type(exc).__name__}: "
            f"{str(exc)[:200]}"
        )

        return False, delay, message

    delay = calculate_retry_delay(
        task,
        NETWORK_RETRY_SECONDS
    )

    message = (
        f"{type(exc).__name__}: "
        f"{str(exc)[:200]}"
    )

    return False, delay, message


# =====================================
# Claim Due Tasks
# =====================================

def claim_due_tasks(limit: int = 5) -> list[dict]:
    """在 SQLite 事务内原子领取到期任务。"""

    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit < 1
    ):
        raise ValueError("limit 必须是正整数")

    init_dispatcher_db()

    now = utc_now()
    now_text = now.isoformat()

    lease_expires = (
        now + timedelta(seconds=LEASE_SECONDS)
    ).isoformat()

    claimed = []

    with closing(
        sqlite3.connect(paths.tasks_db, timeout=10)
    ) as conn:
        conn.row_factory = sqlite3.Row

        with conn:
            conn.execute("BEGIN IMMEDIATE")

            rows = conn.execute(
                """
                SELECT *
                FROM monitor_tasks
                WHERE status = 'active'
                  AND archived_at IS NULL
                  AND (
                      next_run_at IS NULL
                      OR next_run_at <= ?
                  )
                  AND (
                      lease_expires_at IS NULL
                      OR lease_expires_at <= ?
                  )
                ORDER BY next_run_at, id
                LIMIT ?
                """,
                (now_text, now_text, limit)
            ).fetchall()

            for row in rows:
                task = dict(row)
                token = uuid4().hex

                cursor = conn.execute(
                    """
                    UPDATE monitor_tasks
                    SET lease_token = ?,
                        lease_expires_at = ?
                    WHERE id = ?
                      AND status = 'active'
                      AND archived_at IS NULL
                      AND (
                          lease_expires_at IS NULL
                          OR lease_expires_at <= ?
                      )
                    """,
                    (
                        token,
                        lease_expires,
                        task["id"],
                        now_text
                    )
                )

                if cursor.rowcount == 1:
                    task["lease_token"] = token
                    claimed.append(task)

    return claimed


# =====================================
# Complete Task
# =====================================

def complete_task(
    task: dict,
    error: str | None = None,
    pause: bool = False,
    retry_seconds: int | None = None,
    defer_seconds: int | None = None,
) -> bool:
    """有效租约持有者更新任务状态。

    defer_seconds 用于"这一轮先不抓"的顺延（例如同一主机刚被抓过）：
    它既不记为失败，也不重置 last_checked_at。
    """

    now = utc_now()
    now_text = now.isoformat()

    if defer_seconds is not None:
        if (
            error is not None
            or pause
            or retry_seconds is not None
        ):
            raise ValueError(
                "defer_seconds 不能与 error / pause / "
                "retry_seconds 同时使用"
            )

        if (
            isinstance(defer_seconds, bool)
            or not isinstance(defer_seconds, int)
            or defer_seconds < 1
        ):
            raise ValueError(
                "defer_seconds 必须是正整数"
            )

    if retry_seconds is not None:
        if (
            isinstance(retry_seconds, bool)
            or not isinstance(retry_seconds, int)
            or retry_seconds < 1
        ):
            raise ValueError(
                "retry_seconds 必须是正整数"
            )

    if defer_seconds is not None:
        next_run = (
            now + timedelta(seconds=defer_seconds)
        ).isoformat()

    elif error is not None and retry_seconds is not None:
        next_run = (
            now + timedelta(seconds=retry_seconds)
        ).isoformat()
    else:
        next_run = (
            now + timedelta(
                minutes=task["interval_minutes"]
            )
        ).isoformat()

    with closing(
        sqlite3.connect(paths.tasks_db, timeout=10)
    ) as conn:
        with conn:
            if defer_seconds is not None:
                # 顺延：只推下次执行时间，不动失败计数与 last_checked_at。
                cursor = conn.execute(
                    """
                    UPDATE monitor_tasks
                    SET next_run_at = ?,
                        lease_token = NULL,
                        lease_expires_at = NULL
                    WHERE id = ?
                      AND lease_token = ?
                      AND lease_expires_at > ?
                      AND status = 'active'
                      AND archived_at IS NULL
                    """,
                    (
                        next_run,
                        task["id"],
                        task["lease_token"],
                        now_text
                    )
                )

            elif pause:
                cursor = conn.execute(
                    """
                    UPDATE monitor_tasks
                    SET status = 'paused',
                        last_error = ?,
                        next_run_at = NULL,
                        failure_count = failure_count + 1,
                        lease_token = NULL,
                        lease_expires_at = NULL
                    WHERE id = ?
                      AND lease_token = ?
                      AND lease_expires_at > ?
                      AND status = 'active'
                      AND archived_at IS NULL
                    """,
                    (
                        (error or "任务已暂停")[:300],
                        task["id"],
                        task["lease_token"],
                        now_text
                    )
                )

            elif error is not None:
                cursor = conn.execute(
                    """
                    UPDATE monitor_tasks
                    SET last_error = ?,
                        next_run_at = ?,
                        failure_count = failure_count + 1,
                        lease_token = NULL,
                        lease_expires_at = NULL
                    WHERE id = ?
                      AND lease_token = ?
                      AND lease_expires_at > ?
                      AND status = 'active'
                      AND archived_at IS NULL
                    """,
                    (
                        error[:300],
                        next_run,
                        task["id"],
                        task["lease_token"],
                        now_text
                    )
                )

            else:
                cursor = conn.execute(
                    """
                    UPDATE monitor_tasks
                    SET last_checked_at = ?,
                        next_run_at = ?,
                        last_error = NULL,
                        failure_count = 0,
                        lease_token = NULL,
                        lease_expires_at = NULL
                    WHERE id = ?
                      AND lease_token = ?
                      AND lease_expires_at > ?
                      AND status = 'active'
                      AND archived_at IS NULL
                    """,
                    (
                        now_text,
                        next_run,
                        task["id"],
                        task["lease_token"],
                        now_text
                    )
                )

            updated = cursor.rowcount == 1

    if not updated:
        logger.warning(
            "任务 %s 未能更新：租约失效或任务已停止",
            task["id"]
        )

    return updated


# =====================================
# Safe Database Operations
# =====================================

def safe_complete_task(
    task: dict,
    error: str | None = None,
    pause: bool = False,
    retry_seconds: int | None = None,
    defer_seconds: int | None = None,
) -> bool:
    try:
        return complete_task(
            task,
            error=error,
            pause=pause,
            retry_seconds=retry_seconds,
            defer_seconds=defer_seconds,
        )
    except Exception:
        logger.exception(
            "任务 %s 更新状态失败",
            task["id"]
        )
        return False


def safe_finish_run(
    run_id: str,
    status: str,
    new_items: int = 0,
    error_message: str | None = None
) -> bool:
    try:
        result = finish_task_run(
            run_id,
            status=status,
            new_items=new_items,
            error_message=error_message
        )

        if not result:
            logger.warning(
                "执行历史 %s 未能更新",
                run_id
            )

        return result

    except Exception:
        logger.exception(
            "执行历史 %s 写入失败",
            run_id
        )
        return False


# =====================================
# Execute Due Tasks
# =====================================

def run_due_monitor_tasks() -> int:
    """执行到期监控，并记录成功或失败历史。"""

    tasks = claim_due_tasks(limit=5)

    for task in tasks:
        task_id = task["id"]
        source_url = task["source_url"]

        # 频率控制：同一台主机刚被抓过就整轮顺延。
        # 放在 start_task_run 之前 —— 顺延等于"这一轮没有开始"，
        # 不该留下执行历史，也不该计入失败次数。
        try:
            host = check_url_shape(source_url)

        except UrlPolicyError:
            # 数据源本身不合法，交给下面的授权分支处理
            host = None

        if host is not None:
            try:
                wait_seconds = reserve_fetch(host)

            except Exception:
                logger.exception(
                    "任务 %s 频率检查失败，本轮跳过",
                    task_id
                )

                safe_complete_task(
                    task,
                    defer_seconds=NETWORK_RETRY_SECONDS
                )
                continue

            if wait_seconds:
                logger.info(
                    "任务 %s 顺延 %s 秒：主机 %s 刚抓过",
                    task_id,
                    wait_seconds,
                    host
                )

                safe_complete_task(
                    task,
                    defer_seconds=wait_seconds
                )
                continue

        try:
            run_id = start_task_run(
                task_id=task_id,
                lease_token=task["lease_token"]
            )
        except Exception as exc:
            logger.exception(
                "任务 %s 创建执行历史失败",
                task_id
            )

            delay = calculate_retry_delay(
                task,
                NETWORK_RETRY_SECONDS
            )

            safe_complete_task(
                task,
                error=(
                    f"HistoryStartError: {str(exc)[:200]}"
                ),
                retry_seconds=delay
            )
            continue

        try:
            # 数据源授权与接口层共用同一份 URL 策略，
            # 避免两处判断漂移（可选域名白名单见 RSS_ALLOWED_HOSTS）。
            check_host_allowed(source_url)

        except UrlPolicyError as error:
            reason = f"数据源未获得执行许可：{error}"

            safe_complete_task(
                task,
                error=reason,
                pause=True
            )

            safe_finish_run(
                run_id,
                status="failed",
                error_message=reason
            )
            continue

        try:
            logger.info(
                "执行 RSS 任务 %s，run_id=%s",
                task_id,
                run_id
            )

            count = run_source(
                source_type=(
                    task.get("task_type")
                    or DEFAULT_SOURCE_TYPE
                ),
                monitor_id=task_id,
                source_url=source_url,
            )

            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
            ):
                raise ValueError(
                    "RSS Monitor 返回了无效数量"
                )

        except Exception as exc:
            logger.exception(
                "任务 %s 执行失败",
                task_id
            )

            pause, delay, error_text = classify_failure(
                task,
                exc
            )

            updated = safe_complete_task(
                task,
                error=error_text,
                pause=pause,
                retry_seconds=delay
            )

            safe_finish_run(
                run_id,
                status="failed",
                error_message=error_text
            )

            if updated:
                logger.warning(
                    "任务 %s 失败，pause=%s，重试延迟=%s",
                    task_id,
                    pause,
                    delay
                )

            continue

        updated = safe_complete_task(task)

        if updated:
            safe_finish_run(
                run_id,
                status="success",
                new_items=count
            )

            logger.info(
                "任务 %s 成功，新增 %s 篇",
                task_id,
                count
            )

        else:
            safe_finish_run(
                run_id,
                status="failed",
                error_message=(
                    "RSS 已执行，但完成状态无法确认；"
                    "可能存在租约过期或数据库异常"
                )
            )

    return len(tasks)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print(
        "Due tasks:",
        run_due_monitor_tasks()
    )