"""Qwen 受控 Runner：进程生命周期 + 结构化终态 + 产物回收。

边界（写死）：
- `workspace` 只限制初始 cwd 与产物回收范围，**不是 OS 沙箱**；
- Provider 成功只进入 `completed_unverified`，业务成功由上层 Verify 决定；
- 杀树/回收无法确认时一律 `unknown`，不猜。
"""

from __future__ import annotations

import os
import subprocess
import time

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from src.collab.qwen_events import (
    EventError,
    check_model,
    check_session,
    is_success,
    parse_events,
    usage_of,
)
from src.collab.qwen_workspace import ArtifactError, RunWorkspace


DEFAULT_QWEN = r"C:\Users\123\AppData\Roaming\npm\qwen.cmd"
ENV_WHITELIST = (
    "PATH", "PATHEXT", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP",
    "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "COMSPEC",
)


@dataclass
class RunSpec:
    run_id: str
    provider_session_id: str
    prompt: str
    model: str
    approval_mode: str = "plan"
    expected_artifacts: dict[str, bool] = field(default_factory=dict)
    max_wall_time: str = "60s"
    max_session_turns: int = 8
    max_tool_calls: int = 20
    timeout_seconds: float = 90.0
    prompt_transport: str = "arg"  # stdin 实测不可用；默认 arg
    safe_mode: bool = True
    bare: bool = False  # --bare 会跳过 settings（无 auth），默认关闭


class QwenRunner:
    def __init__(self, *, root: str | Path = "data/qwen-runs", qwen_cmd: str = DEFAULT_QWEN, env=None):
        self.root = Path(root)
        self.qwen_cmd = qwen_cmd
        self._env = env

    def build_command(self, spec: RunSpec) -> list[str]:
        command = [
            self.qwen_cmd,
            "--session-id", spec.provider_session_id,
            "--output-format", "json",
            "--model", spec.model,
            "--approval-mode", spec.approval_mode,
            "--max-wall-time", spec.max_wall_time,
            "--max-session-turns", str(spec.max_session_turns),
            "--max-tool-calls", str(spec.max_tool_calls),
        ]

        if spec.safe_mode:
            command.append("--safe-mode")

        if spec.bare:
            command.append("--bare")

        if spec.prompt_transport == "arg":
            command.insert(1, spec.prompt)

        return command

    def build_env(self) -> dict:
        source = self._env if self._env is not None else os.environ
        env = {key: source[key] for key in ENV_WHITELIST if key in source}
        env["NO_BROWSER"] = "1"

        return env

    # ---------- 主流程 ----------

    def execute(self, spec: RunSpec) -> dict:
        workspace = RunWorkspace(self.root, spec.run_id).create()
        started = datetime.now(timezone.utc)
        state = {
            "schema_version": 1,
            "run_id": spec.run_id,
            "provider_session_id": spec.provider_session_id,
            "state": "running",
            "reason": None,
            "requested_model": spec.model,
            "observed_model": None,
            "approval_mode": spec.approval_mode,
            "pid": None,
            "process_started_at": started.isoformat(),
            "started_at": started.isoformat(),
            "finished_at": None,
            "duration_ms": None,
            "exit_code": None,
            "usage": {"input_tokens": None, "output_tokens": None, "total_tokens": None},
            "manifest_path": None,
            "cleanup": {"required": False, "kill_attempted": False, "process_reaped": None},
        }
        workspace.write_state(state)
        monotonic_start = time.monotonic()

        process = subprocess.Popen(
            self.build_command(spec),
            cwd=str(workspace.workspace),
            env=self.build_env(),
            stdin=subprocess.PIPE if spec.prompt_transport == "stdin" else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        state["pid"] = process.pid
        workspace.write_state(state)

        timed_out = False

        try:
            stdout, stderr = process.communicate(
                input=spec.prompt if spec.prompt_transport == "stdin" else None,
                timeout=spec.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            stdout, stderr = self._kill_and_reap(process, state, workspace)
        except KeyboardInterrupt:
            stdout, stderr = self._kill_and_reap(process, state, workspace)
            state["state"] = "cancelled" if state["cleanup"]["process_reaped"] else "unknown"
            _finish(state, monotonic_start)
            workspace.write_state(state)

            return state

        exit_code = process.returncode
        state["cleanup"]["process_reaped"] = process.poll() is not None
        _finish(state, monotonic_start)
        state["exit_code"] = exit_code

        if timed_out:
            state["state"] = "timeout" if state["cleanup"]["process_reaped"] else "unknown"
            state["reason"] = {"code": "watchdog_timeout", "message": "外层看门狗触发"}
            workspace.write_state(state)

            return state

        if not state["cleanup"]["process_reaped"]:
            state["state"] = "unknown"
            state["reason"] = {"code": "cleanup_failed", "message": "无法确认进程回收"}
            workspace.write_state(state)

            return state

        result = self._judge(state, workspace, stdout, exit_code, spec)
        workspace.write_state(result)

        return result

    def _judge(self, state: dict, workspace: RunWorkspace, stdout: str, exit_code, spec: RunSpec) -> dict:
        try:
            parsed = parse_events(stdout)
        except EventError as error:
            state["state"] = "failed"
            message = str(error)[:200]

            try:
                data = __import__("json").loads(stdout)
                first_error = data[-1].get("error") if isinstance(data, list) and data else None

                if isinstance(first_error, dict) and first_error.get("message"):
                    state["reason"] = {"code": "provider_error", "message": str(first_error["message"])[:200]}

                    return state
            except Exception:
                pass

            state["reason"] = {"code": error.code, "message": message}

            return state

        state["observed_model"] = parsed["init"].get("model")
        state["usage"] = usage_of(parsed)

        if exit_code != 0:
            state["state"] = "failed"
            state["reason"] = {"code": "provider_error", "message": f"exit={exit_code}"}

            return state

        try:
            check_session(parsed, spec.provider_session_id)
            check_model(parsed, spec.model)
        except EventError as error:
            state["state"] = "failed"
            state["reason"] = {"code": error.code, "message": str(error)[:200]}

            return state

        if not is_success(parsed):
            state["state"] = "failed"
            state["reason"] = {"code": "provider_error", "message": "result 非成功"}

            return state

        try:
            manifest = workspace.scan(spec.expected_artifacts)
        except ArtifactError as error:
            state["state"] = "failed"
            state["reason"] = {"code": error.code, "message": str(error)[:200]}

            return state

        state["state"] = "completed_unverified"
        state["manifest_path"] = str(workspace.manifest_path)

        return state

    def _kill_and_reap(self, process, state: dict, workspace: RunWorkspace):
        state["cleanup"]["required"] = True
        state["cleanup"]["kill_attempted"] = True
        workspace.write_state(state)

        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            pass

        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            state["cleanup"]["process_reaped"] = False

            return "", ""

        state["cleanup"]["process_reaped"] = process.poll() is not None

        return stdout, stderr


def _finish(state: dict, monotonic_start: float) -> None:
    state["finished_at"] = datetime.now(timezone.utc).isoformat()
    state["duration_ms"] = int((time.monotonic() - monotonic_start) * 1000)


def status_of(root: str | Path, run_id: str) -> dict | None:
    workspace = RunWorkspace(root, run_id)
    state = workspace.read_state()

    if state is None:
        return None

    if state.get("state") == "running" and not _pid_alive(state.get("pid")):
        state["state"] = "unknown"
        state["reason"] = {"code": "stale_running", "message": "进程已不存在"}
        workspace.write_state(state)

    return state


def _pid_alive(pid) -> bool:
    if not pid:
        return False

    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return True

    return str(int(pid)) in (result.stdout or "")
