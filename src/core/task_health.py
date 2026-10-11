
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from src.core.task_engine import DB_PATH


def utc_now():
    return datetime.now(timezone.utc)


def parse_utc(value):
    """解析数据库里的时间，统一为 UTC。"""

    if not isinstance(value, str) or not value:
        return None

    try:
        value = value.replace("Z", "+00:00")
        result = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None

    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)

    return result.astimezone(timezone.utc)


def scan_task_health(
    failure_threshold: int = 3,
    stale_seconds: int = 600,
    limit: int = 200,
) -> list[dict]:
    """
    扫描后台任务健康状态。

    仅查询数据库，不修改任何任务。

    检查：
    1. 连续失败达到阈值
    2. 因错误而暂停的任务
    3. 长时间未结束的执行历史
    4. 租约丢失、过期或被其他 Worker 接管
    """

    if (
        isinstance(failure_threshold, bool)
        or not isinstance(failure_threshold, int)
        or failure_threshold < 1
    ):
        raise ValueError("failure_threshold 必须为正整数")

    if (
        isinstance(stale_seconds, bool)
        or not isinstance(stale_seconds, int)
        or stale_seconds < 1
    ):
        raise ValueError("stale_seconds 必须为正整数")

    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 1000
    ):
        raise ValueError("limit 必须在 1 到 1000 之间")

    now = utc_now()
    findings = []

    with closing(
        sqlite3.connect(DB_PATH, timeout=10)
    ) as conn:
        conn.row_factory = sqlite3.Row

        # -----------------------------
        # 连续失败与异常暂停
        # -----------------------------

        failed_tasks = conn.execute(
            """
            SELECT
                id,
                owner_id,
                status,
                failure_count,
                last_error
            FROM monitor_tasks
            WHERE archived_at IS NULL
              AND (
                  failure_count >= ?
                  OR (
                      status = 'paused'
                      AND last_error IS NOT NULL
                  )
              )
            ORDER BY failure_count DESC
            LIMIT ?
            """,
            (failure_threshold, limit)
        ).fetchall()

        for row in failed_tasks:
            issue_type = (
                "paused_error"
                if row["status"] == "paused"
                else "repeated_failures"
            )

            findings.append({
                "type": issue_type,
                "task_id": row["id"],
                "owner_id": row["owner_id"],
                "failure_count": row["failure_count"],
                "last_error": (
                    row["last_error"] or ""
                )[:200],
            })

        # -----------------------------
        # 长时间未结束的运行记录
        # -----------------------------

        running = conn.execute(
            """
            SELECT
                h.run_id,
                h.task_id,
                h.lease_token AS run_token,
                h.started_at,
                m.id AS existing_task_id,
                m.owner_id,
                m.status AS task_status,
                m.archived_at,
                m.lease_token AS current_token,
                m.lease_expires_at
            FROM task_run_history AS h
            LEFT JOIN monitor_tasks AS m
              ON m.id = h.task_id
            WHERE h.status = 'running'
            ORDER BY h.started_at ASC
            LIMIT ?
            """,
            (limit,)
        ).fetchall()

        for row in running:
            started = parse_utc(row["started_at"])

            if started is None:
                continue

            age_seconds = (
                now - started
            ).total_seconds()

            # 不误报刚启动的任务
            if age_seconds < stale_seconds:
                continue

            reason = None

            if row["existing_task_id"] is None:
                reason = "task_missing"

            elif (
                row["archived_at"] is not None
                or row["task_status"] != "active"
            ):
                reason = "task_inactive"

            elif row["current_token"] is None:
                reason = "lease_cleared"

            elif (
                row["current_token"]
                != row["run_token"]
            ):
                reason = "lease_replaced"

            else:
                expires = parse_utc(
                    row["lease_expires_at"]
                )

                if expires is None:
                    reason = "lease_expiry_missing"

                elif expires <= now:
                    reason = "lease_expired"

            if reason is None:
                continue

            findings.append({
                "type": "suspected_stuck_run",
                "reason": reason,
                "task_id": row["task_id"],
                "run_id": row["run_id"],
                "owner_id": row["owner_id"],
                "age_seconds": int(age_seconds),
                "started_at": row["started_at"],
            })

    return findings


if __name__ == "__main__":
    import json

    try:
        issues = scan_task_health()

        print(
            json.dumps(
                issues,
                ensure_ascii=False,
                indent=2
            )
        )

        print(f"发现可疑记录：{len(issues)} 条")

    except sqlite3.Error as exc:
        print(f"数据库健康检查失败：{exc}")
        raise SystemExit(1)