"""Qwen 受控 Runner：进程生命周期 + 结构化终态 + 产物回收。

边界（写死）：
- `workspace` 只限制初始 cwd 与产物回收范围，**不是 OS 沙箱**；
- Provider 成功只进入 `completed_unverified`，业务成功由上层 Verify 决定；
- 父进程回收 ≠ 整棵进程树收口；无法确认时一律 `unknown`。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid

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
from src.collab.qwen_workspace import ArtifactError, RunWorkspace, WorkspaceError


DEFAULT_QWEN = r"C:\Users\123\AppData\Roaming\npm\qwen.cmd"
ENV_WHITELIST = (
    "PATH", "PATHEXT", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP",
    "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "COMSPEC",
)


class RunSpecError(ValueError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


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
    prompt_transport: str = "stdin"  # 实测可用
    safe_mode: bool = True
    bare: bool = False  # --bare 跳过 settings（无 auth），默认关闭


def validate_spec(spec: RunSpec) -> None:
    for name, value in (("run_id", spec.run_id), ("provider_session_id", spec.provider_session_id)):
        try:
            uuid.UUID(str(value))
        except (ValueError, AttributeError, TypeError):
            raise RunSpecError("invalid_uuid", f"{name} 必须是 UUID") from None

    if not (spec.prompt or "").strip():
        raise RunSpecError("empty_prompt", "prompt 不能为空")

    if spec.approval_mode == "yolo" and not spec.expected_artifacts:
        raise RunSpecError("expected_artifacts_required", "yolo 运行必须声明预期产物")

    if spec.approval_mode not in ("plan", "yolo"):
        raise RunSpecError("invalid_approval_mode", spec.approval_mode)


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

        if spec.prompt_transport == "arg":
            command.insert(1, spec.prompt)

        if spec.safe_mode:
            command.append("--safe-mode")

        if spec.bare:
            command.append("--bare")

        return command

    def build_env(self) -> dict:
        source = self._env if self._env is not None else os.environ
        env = {key: source[key] for key in ENV_WHITELIST if key in source}
        env["NO_BROWSER"] = "1"

        return env

    # ---------- 主流程 ----------

    def execute(self, spec: RunSpec) -> dict:
        validate_spec(spec)

        try:
            workspace = RunWorkspace(self.root, spec.run_id).create()
        except WorkspaceError as error:
            raise RunSpecError(error.code, str(error)) from None

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
            "cleanup": {
                "required": False,
                "kill_attempted": False,
                "parent_reaped": None,
                "tree_cleanup_confirmed": None,
            },
        }
        workspace.write_state(state)
        monotonic_start = time.monotonic()

        try:
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
        except OSError as error:
            _finish(state, monotonic_start)
            state["state"] = "failed"
            state["reason"] = {"code": "provider_error", "message": str(error)[:200]}
            workspace.write_state(state)

            return state

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
            stdout, _ = self._kill_and_reap(process, state, workspace)
        except KeyboardInterrupt:
            stdout, _ = self._kill_and_reap(process, state, workspace)
            _finish(state, monotonic_start)
            state["state"] = "cancelled" if state["cleanup"]["tree_cleanup_confirmed"] else "unknown"
            workspace.write_state(state)

            return state

        exit_code = process.returncode
        state["cleanup"]["parent_reaped"] = process.poll() is not None
        _finish(state, monotonic_start)
        state["exit_code"] = exit_code

        if timed_out:
            if state["cleanup"]["parent_reaped"] and state["cleanup"]["tree_cleanup_confirmed"]:
                state["state"] = "timeout"
                state["reason"] = {"code": "watchdog_timeout", "message": "外层看门狗触发"}
            else:
                state["state"] = "unknown"
                state["reason"] = {"code": "cleanup_failed", "message": "无法确认整棵进程树回收"}

            workspace.write_state(state)

            return state

        if not state["cleanup"]["parent_reaped"]:
            state["state"] = "unknown"
            state["reason"] = {"code": "cleanup_failed", "message": "无法确认进程回收"}
            workspace.write_state(state)

            return state

        result = self._judge(state, workspace, stdout, exit_code, spec)
        workspace.write_state(result)

        return result

    def _judge(self, state: dict, workspace: RunWorkspace, stdout: str, exit_code, spec: RunSpec) -> dict:
        if exit_code == 55:
            state["state"] = "failed"
            state["reason"] = {"code": "native_limit", "message": "原生墙钟/工具调用上限（exit 55）"}

            return state

        try:
            parsed = parse_events(stdout)
        except EventError as error:
            state["state"] = "failed"
            provider_message = _provider_error_message(stdout)
            state["reason"] = (
                {"code": "provider_error", "message": provider_message}
                if provider_message
                else {"code": error.code, "message": str(error)[:200]}
            )

            return state

        state["observed_model"] = parsed["init"].get("model")

        try:
            state["usage"] = usage_of(parsed)
        except EventError as error:
            state["state"] = "failed"
            state["reason"] = {"code": error.code, "message": str(error)[:200]}

            return state

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
        kill_ok = False

        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
            )
            kill_ok = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            kill_ok = False

        try:
            stdout, _ = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            state["cleanup"]["parent_reaped"] = False
            state["cleanup"]["tree_cleanup_confirmed"] = False

            return "", False

        parent_reaped = process.poll() is not None
        tree_gone = _pid_exists(process.pid)
        state["cleanup"]["parent_reaped"] = parent_reaped
        state["cleanup"]["tree_cleanup_confirmed"] = bool(kill_ok and parent_reaped and tree_gone is False)

        return stdout, state["cleanup"]["tree_cleanup_confirmed"]


def _finish(state: dict, monotonic_start: float) -> None:
    state["finished_at"] = datetime.now(timezone.utc).isoformat()
    state["duration_ms"] = int((time.monotonic() - monotonic_start) * 1000)


def _provider_error_message(stdout: str) -> str:
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return ""

    if not isinstance(data, list) or not data:
        return ""

    error = data[-1].get("error") if isinstance(data[-1], dict) else None

    return str(error.get("message"))[:200] if isinstance(error, dict) and error.get("message") else ""


def _pid_exists(pid) -> bool | None:
    """精确判断 PID 是否存在；查询失败返回 None。"""

    if not pid:
        return False

    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH", "/FI", f"PID eq {int(pid)}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if result.returncode != 0:
        return None

    for line in (result.stdout or "").splitlines():
        cells = [cell.strip('"') for cell in line.split('","')]

        if cells and cells[0].strip('"').isdigit() and int(cells[0].strip('"')) == int(pid):
            return True

    return False


def _pid_alive(pid, started_at: str | None) -> bool | None:
    exists = _pid_exists(pid)

    if exists is not True or not started_at:
        return exists

    creation = _process_creation_time(pid)

    if creation is None:
        return None

    try:
        expected = datetime.fromisoformat(started_at)
    except ValueError:
        return None

    if expected.tzinfo is None:
        expected = expected.replace(tzinfo=timezone.utc)

    if abs((creation - expected).total_seconds()) > 5:
        return False

    return True


def _process_creation_time(pid) -> datetime | None:
    command = [
        "powershell", "-NoProfile", "-Command",
        f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CreationDate.ToString('o')",
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None

    text = (result.stdout or "").strip()

    if result.returncode != 0 or not text:
        return None

    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def status_of(root: str | Path, run_id: str) -> dict | None:
    try:
        uuid.UUID(str(run_id))
    except (ValueError, AttributeError, TypeError):
        raise RunSpecError("invalid_uuid", "run_id 必须是 UUID") from None

    workspace = RunWorkspace(root, run_id)
    state = workspace.read_state()

    if state is None:
        return None

    if state.get("state") == "running":
        alive = _pid_alive(state.get("pid"), state.get("process_started_at"))

        if alive is not True:
            state["state"] = "unknown"
            state["reason"] = {"code": "stale_running", "message": "进程不存在或无法确认身份"}
            workspace.write_state(state)

    return state
