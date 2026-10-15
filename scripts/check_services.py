r"""Service self-check for Personal Agent.

Prints a one-screen diagnosis of the four processes the project needs:

    API        uvicorn src.main:app
    Scheduler  python -m src.scheduler
    Outbox     python -m src.core.outbox_service
    Frontend   npm run dev  (development only)

It does NOT inspect process lists. It checks the *symptoms* in the data,
because those are what actually tell you a service is missing:

- pending outbox rows that are old   -> the outbox service is not running
- active tasks overdue for a long time -> the scheduler is not running
- /health unreachable                  -> the API is not running

Output is English on purpose: this Windows console is GBK, and Chinese
output from Python turns into mojibake in it.

Usage:
    .\.venv\Scripts\python.exe scripts\check_services.py
"""

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

# 让 `python scripts/check_services.py` 也能 import src：
# 直接跑脚本时 sys.path[0] 是 scripts/，不是仓库根目录。
# 与 scripts/run_tests.py 同一套做法。
ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.chdir(ROOT)

from src.config import DATA_DIR_ENV, paths


API_BASE = os.getenv("PA_API_BASE", "http://127.0.0.1:8000")
FRONTEND_BASE = os.getenv("PA_FRONTEND_BASE", "http://127.0.0.1:5173")

# 系统里可能残留一个指向死端口的代理配置，
# Python 的 urllib 会读它 —— 显式绕开，避免自检自己卡住。
OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({})
)

STALE_PENDING_SECONDS = 120
OVERDUE_SECONDS = 180


def reachable(url: str) -> bool:
    try:
        with OPENER.open(url, timeout=3) as response:
            return 200 <= response.status < 400

    except Exception:
        return False


def api_ok() -> bool:
    try:
        with OPENER.open(
            f"{API_BASE}/health", timeout=3
        ) as response:
            body = json.loads(response.read().decode())

        return body.get("status") == "ok"

    except Exception:
        return False


def parse_time(value) -> datetime | None:
    if not value:
        return None

    try:
        moment = datetime.fromisoformat(value)

    except (TypeError, ValueError):
        return None

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    return moment


def outbox_backlog() -> tuple[int, int]:
    """待投递条数与最老那条的年龄（秒）。"""

    path = Path(paths.tasks_db)

    if not path.is_file():
        return (0, 0)

    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute(
            """
            SELECT created_at
            FROM notification_outbox
            WHERE status = 'pending'
            """
        ).fetchall()

    if not rows:
        return (0, 0)

    now = datetime.now(timezone.utc)

    ages = [
        (now - moment).total_seconds()
        for moment in (parse_time(row[0]) for row in rows)
        if moment is not None
    ]

    return (len(rows), int(max(ages)) if ages else 0)


def task_counts() -> dict:
    path = Path(paths.tasks_db)

    if not path.is_file():
        return {
            "active": 0,
            "paused": 0,
            "archived": 0,
            "overdue": 0,
            "inbox": 0,
        }

    now = datetime.now(timezone.utc)

    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row

        counts = {"active": 0, "paused": 0, "archived": 0, "overdue": 0}

        for row in conn.execute(
            """
            SELECT status, next_run_at, archived_at
            FROM monitor_tasks
            """
        ):
            if row["archived_at"]:
                counts["archived"] += 1
                continue

            if row["status"] == "active":
                counts["active"] += 1

                due = parse_time(row["next_run_at"])

                if due is not None and (
                    now - due
                ).total_seconds() > OVERDUE_SECONDS:
                    counts["overdue"] += 1

            elif row["status"] == "paused":
                counts["paused"] += 1

        inbox = conn.execute(
            """
            SELECT COUNT(*) FROM notification_outbox
            WHERE status = 'in_app'
            """
        ).fetchone()[0]

    counts["inbox"] = inbox

    return counts


def line(label: str, text: str) -> None:
    print(f"  {label:<10} {text}")


def main() -> int:
    problems = []

    print("")
    print("Personal Agent - service self-check")
    print("=" * 58)

    data_dir = os.getenv(DATA_DIR_ENV, "").strip()
    line(
        "data dir",
        data_dir if data_dir else "(project root - REAL DATA)",
    )
    line("tasks db", str(paths.tasks_db))

    # ---- API ----

    if api_ok():
        line("api", f"OK        {API_BASE}/health")
    else:
        line("api", f"DOWN      {API_BASE}/health")
        problems.append(
            "API is not running: the panel will not load. Start: uvicorn src.main:app --port 8000"
        )

    # ---- Frontend ----

    if reachable(FRONTEND_BASE):
        line("frontend", f"OK        {FRONTEND_BASE}")
    else:
        line(
            "frontend",
            f"DOWN      {FRONTEND_BASE} (development only)",
        )

    # ---- Outbox service ----

    pending, oldest = outbox_backlog()

    if pending == 0:
        line("outbox", "OK        no pending rows")
    elif oldest > STALE_PENDING_SECONDS:
        line(
            "outbox",
            f"STUCK     {pending} pending, oldest {oldest}s",
        )
        problems.append(
            "outbox service is not running: notifications stay pending "
            "and the in-app inbox stays empty. Start: python -m src.core.outbox_service"
        )
    else:
        line(
            "outbox",
            f"BUSY      {pending} pending, oldest {oldest}s (within tolerance)",
        )

    # ---- Scheduler ----

    counts = task_counts()

    if counts["overdue"] == 0:
        line("scheduler", "OK        no overdue tasks")
    else:
        line(
            "scheduler",
            f"STUCK     {counts['overdue']} overdue tasks",
        )
        problems.append(
            "scheduler is not running: due tasks are never picked up. Start: python -m src.scheduler"
        )

    # ---- Delivery config ----

    topic = os.getenv("NTFY_TOPIC", "").strip()

    line(
        "ntfy",
        "configured (owner notifications push to phone)"
        if topic
        else "NOT configured (owner notifications will fail, in-app still works)",
    )

    # ---- Data overview ----

    line(
        "tasks",
        "active {active} / paused {paused} / archived {archived}".format(
            **counts
        ),
    )
    line("inbox", f"{counts['inbox']} pending in-app items")

    print("=" * 58)

    if problems:
        print("PROBLEMS: %d" % len(problems))
        for index, problem in enumerate(problems, 1):
            print("  %d. %s" % (index, problem))
        print("")
        return 1

    print("all checks passed.")
    print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
