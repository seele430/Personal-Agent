"""QQ 同任务低风险继续：run 解析 + relay continue 调用。

边界：
- 只允许 `succeeded` 的 run；
- 只传文本，不新增工具/权限；
- 错误信息不回显用户消息正文。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys

from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
PREFIX_RE = re.compile(r"^[0-9a-f-]{8,36}$", re.I)


class ContinueError(ValueError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def db_path() -> Path:
    return Path(os.getenv("PA_COLLAB_DB") or ROOT / "data" / "collab-runs.db")


def resolve_run(token: str, *, path: str | Path | None = None) -> tuple[str, str]:
    token = (token or "").strip()

    if not token:
        raise ContinueError("missing", "缺少 run_id")

    if not (UUID_RE.match(token) or PREFIX_RE.match(token)):
        raise ContinueError("invalid", "run_id 格式不合法")

    database = Path(path or db_path())

    if not database.exists():
        raise ContinueError("not_found", "没有可继续的任务")

    with closing(sqlite3.connect(database, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row

        if UUID_RE.match(token):
            rows = connection.execute(
                "SELECT run_id, state FROM collab_runs WHERE run_id=?", (token,)
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT run_id, state FROM collab_runs WHERE run_id LIKE ? LIMIT 5",
                (f"{token}%",),
            ).fetchall()

    if not rows:
        raise ContinueError("not_found", "找不到该任务")

    if len(rows) > 1:
        raise ContinueError("ambiguous", "前缀不唯一，请给完整 run_id")

    if rows[0]["state"] != "succeeded":
        raise ContinueError("not_succeeded", f"当前状态 {rows[0]['state']}，不能继续")

    return rows[0]["run_id"], rows[0]["state"]


def continue_run(token: str, message: str, *, timeout: float = 185.0, path: str | Path | None = None) -> dict:
    run_id, _state = resolve_run(token, path=path)
    command = [
        sys.executable,
        str(ROOT / "scripts" / "collab_relay.py"),
        "continue",
        "--run-id", run_id,
        "--message", message,
    ]

    try:
        completed = subprocess.run(
            command,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ContinueError("timeout", "继续超时") from None
    except OSError:
        raise ContinueError("relay_failed", "无法启动继续任务") from None

    try:
        payload = json.loads((completed.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        raise ContinueError("relay_failed", "继续结果不可读") from None

    if completed.returncode != 0 or payload.get("error"):
        raise ContinueError("relay_failed", "继续失败")

    if not isinstance(payload, dict):
        raise ContinueError("relay_failed", "继续结果格式不正确")

    return payload
