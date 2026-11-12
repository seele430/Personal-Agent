"""DSH headless 受控 Runner：启动、事件解析、超时杀进程树。

边界（写死）：

- 只跑 `dsh headless --json`；cwd 固定为调用方给的 run workspace；
- 环境变量白名单 + `DEEPSEEK_API_KEY`，不继承全部环境；
- 超时 / 非零退出 / 无 `final` → 一律失败，**不猜成功**；
- 只认 `final.text` 与 `final.reason.kind`；流式 `text` 片段不作判定依据。
"""

from __future__ import annotations

import json
import os
import subprocess
import time

from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_DSH = Path(
    r"C:\Users\123\AppData\Local\Programs\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd"
)
ENV_WHITELIST = (
    "PATH", "PATHEXT", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP",
    "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "COMSPEC",
)
DEFAULT_TIMEOUT = 600.0
MAX_OUTPUT_CHARS = 2 * 1024 * 1024


@dataclass
class RunResult:
    ok: bool = False
    text: str = ""
    reason: str = ""
    exit_code: int | None = None
    elapsed_s: float = 0.0
    usage: dict = field(default_factory=dict)
    session_id: str = ""
    events: int = 0
    error: str = ""


def build_env(extra: dict | None = None, source: dict | None = None) -> dict:
    source = source if source is not None else os.environ
    env = {key: source[key] for key in ENV_WHITELIST if key in source}
    env["NO_BROWSER"] = "1"

    # DSH headless 的凭据：显式透传，但只认这一个名字
    credential = str(source.get("DEEPSEEK_API_KEY") or "").strip()

    if credential:
        env["DEEPSEEK_API_KEY"] = credential

    for key, value in (extra or {}).items():
        if value:
            env[key] = value

    return env


def parse_events(stdout: str) -> dict:
    parsed = {
        "text": "", "reason": "", "usage": {}, "session_id": "",
        "events": 0, "bad_lines": 0,
    }

    for line in (stdout or "").splitlines():
        line = line.strip()

        if not line:
            continue

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            parsed["bad_lines"] += 1
            continue

        parsed["events"] += 1
        kind = event.get("type")

        if kind == "session":
            parsed["session_id"] = str(event.get("sessionId") or "")
        elif kind == "status" and event.get("phase") == "step_end":
            usage = event.get("usage") or {}

            if usage:
                parsed["usage"] = usage
        elif kind == "final":
            parsed["text"] = str(event.get("text") or "")
            parsed["reason"] = str((event.get("reason") or {}).get("kind") or "")
            parsed["session_id"] = str(event.get("sessionId") or parsed["session_id"])

    return parsed


def _kill_tree(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def run(
    prompt: str,
    workspace: str | Path,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    dsh_path: str | Path = DEFAULT_DSH,
    extra_env: dict | None = None,
) -> RunResult:
    text = (prompt or "").strip()

    if not text:
        return RunResult(ok=False, error="empty_prompt")

    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    command = [
        str(dsh_path),
        "headless",
        "--json",
        "--permission-mode", "workspace-write",
        "--add-dir", str(workspace),
        "-",
    ]

    started = time.monotonic()

    try:
        process = subprocess.Popen(
            command,
            cwd=str(workspace),
            env=build_env(extra_env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as error:
        return RunResult(ok=False, error=f"spawn_failed: {error}", elapsed_s=0.0)

    try:
        stdout, stderr = process.communicate(input=text, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(process.pid)
        try:
            process.kill()
        except OSError:
            pass

        try:
            process.communicate(timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            pass

        return RunResult(
            ok=False,
            elapsed_s=round(time.monotonic() - started, 1),
            error=f"timeout>{timeout}s",
        )

    elapsed = round(time.monotonic() - started, 1)
    parsed = parse_events(stdout[:MAX_OUTPUT_CHARS])
    reason = parsed["reason"]
    ok = process.returncode == 0 and reason == "completed" and bool(parsed["text"].strip())
    error = ""

    if process.returncode != 0:
        error = f"exit={process.returncode}: {(stderr or '')[-200:]}"
    elif reason != "completed":
        error = f"reason={reason or 'missing'}"
    elif not parsed["text"].strip():
        error = "empty_final_text"

    return RunResult(
        ok=ok,
        text=parsed["text"],
        reason=reason,
        exit_code=process.returncode,
        elapsed_s=elapsed,
        usage=parsed["usage"],
        session_id=parsed["session_id"],
        events=parsed["events"],
        error=error,
    )
