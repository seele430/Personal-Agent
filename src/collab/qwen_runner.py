"""Qwen 受控 Runner：进程生命周期 + 结构化终态 + 产物回收。

边界（写死）：
- `workspace` 只限制初始 cwd 与产物回收范围，**不是 OS 沙箱**；
- Provider 成功只进入 `completed_unverified`，业务成功由上层 Verify 决定；
- 父进程回收 ≠ 整棵进程树收口；无法确认时一律 `unknown`。
"""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import threading
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


class _Tracking:
    """Popen 后立即启动的后代跟踪器；只并集，不覆盖。"""

    def __init__(self, pid: int, interval: float = 0.3):
        self.pid = int(pid)
        self.interval = interval
        self.tracked: dict[int, str] = {}
        self.ok = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> "_Tracking":
        self._thread.start()

        return self

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            snapshot = _snapshot_processes()

            if snapshot is None:
                continue

            self.ok = True

            for key, value in _descendants(self.pid, snapshot).items():
                self.tracked.setdefault(key, value)

    def stop(self) -> dict[int, str]:
        self._stop.set()
        self._thread.join(timeout=2)

        return dict(self.tracked)


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
    model_aliases: dict[str, str] = field(default_factory=dict)


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


def transition(state: dict, new_state: str) -> None:
    if state.get("state") != "running" or new_state not in (
        "completed_unverified", "failed", "timeout", "cancelled", "unknown",
    ):
        raise RuntimeError(f"非法状态迁移：{state.get('state')} -> {new_state}")

    state["state"] = new_state


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
            "process_created_at": None,
            "cleanup": {
                "required": False,
                "kill_attempted": False,
                "parent_reaped": None,
                "descendants_seen": None,
                "tree_cleanup_confirmed": None,
            },
        }
        try:
            workspace.write_state(state)
        except OSError as error:
            raise RunSpecError("state_write_failed", str(error)[:200]) from None

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
            _mark(state, "failed")
            state["reason"] = {"code": "provider_error", "message": str(error)[:200]}
            workspace.write_state(state)

            return state

        state["pid"] = process.pid
        state["process_created_at"] = _creation_iso(process.pid)

        try:
            workspace.write_state(state)
        except OSError as error:
            self._kill_and_reap(process, state, workspace)

            raise RunSpecError("state_write_failed", str(error)[:200]) from None

        tracking = _Tracking(process.pid).start()

        if not state["process_created_at"]:
            self._kill_and_reap(process, state, workspace, tracking.stop())
            _finish(state, monotonic_start)
            _mark(state, "unknown", {"code": "cleanup_failed", "message": "无法取得进程创建时间"})
            workspace.write_state(state)

            return state
        timed_out = False

        try:
            stdout, stderr = process.communicate(
                input=spec.prompt if spec.prompt_transport == "stdin" else None,
                timeout=spec.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            stdout, _ = self._kill_and_reap(process, state, workspace, tracking.stop())
        except KeyboardInterrupt:
            stdout, _ = self._kill_and_reap(process, state, workspace, tracking.stop())
            _finish(state, monotonic_start)
            _mark(state, "cancelled" if state["cleanup"]["tree_cleanup_confirmed"] else "unknown")
            workspace.write_state(state)

            return state

        exit_code = process.returncode
        state["cleanup"]["parent_reaped"] = process.poll() is not None
        _finish(state, monotonic_start)
        state["exit_code"] = exit_code

        if timed_out:
            if state["cleanup"]["parent_reaped"] and state["cleanup"]["tree_cleanup_confirmed"]:
                _mark(state, "timeout")
                state["reason"] = {"code": "watchdog_timeout", "message": "外层看门狗触发"}
            else:
                _mark(state, "unknown")
                state["reason"] = {"code": "cleanup_failed", "message": "无法确认整棵进程树回收"}

            workspace.write_state(state)

            return state

        if not state["cleanup"]["parent_reaped"]:
            _mark(state, "unknown")
            state["reason"] = {"code": "cleanup_failed", "message": "无法确认进程回收"}
            workspace.write_state(state)

            return state

        tracked = tracking.stop()
        final = _snapshot_processes()

        if final is not None:
            for key, value in _descendants(process.pid, final).items():
                tracked.setdefault(key, value)

        tree_gone, count, tracked = _wait_tree_gone(process.pid, tracked)
        state["cleanup"]["descendants_seen"] = count
        state["cleanup"]["tree_cleanup_confirmed"] = tree_gone

        if not tree_gone:
            _mark(state, "unknown")
            state["reason"] = {"code": "cleanup_failed", "message": "父进程已退出但仍有后代存活"}
            workspace.write_state(state)

            return state

        result = self._judge(state, workspace, stdout, exit_code, spec)
        workspace.write_state(result)

        return result

    def _judge(self, state: dict, workspace: RunWorkspace, stdout: str, exit_code, spec: RunSpec) -> dict:
        if exit_code == 55:
            _mark(state, "failed")
            state["reason"] = {"code": "native_limit", "message": "原生墙钟/工具调用上限（exit 55）"}

            return state

        try:
            parsed = parse_events(stdout)
        except EventError as error:
            _mark(state, "failed")
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
            _mark(state, "failed")
            state["reason"] = {"code": error.code, "message": str(error)[:200]}

            return state

        if exit_code != 0:
            _mark(state, "failed")
            state["reason"] = {"code": "provider_error", "message": f"exit={exit_code}"}

            return state

        try:
            check_session(parsed, spec.provider_session_id)
            check_model(parsed, spec.model, aliases=spec.model_aliases)
        except EventError as error:
            _mark(state, "failed")
            state["reason"] = {"code": error.code, "message": str(error)[:200]}

            return state

        if not is_success(parsed):
            _mark(state, "failed")
            state["reason"] = {"code": "provider_error", "message": "result 非成功"}

            return state

        try:
            manifest = workspace.scan(spec.expected_artifacts)
        except ArtifactError as error:
            _mark(state, "failed")
            state["reason"] = {"code": error.code, "message": str(error)[:200]}

            return state

        _mark(state, "completed_unverified")
        state["manifest_path"] = str(workspace.manifest_path)

        return state

    def _kill_and_reap(self, process, state: dict, workspace: RunWorkspace, tracked: dict | None = None):
        state["cleanup"]["required"] = True
        state["cleanup"]["kill_attempted"] = True

        try:
            workspace.write_state(state)
        except OSError:
            pass
        kill_ok = False
        tracked = dict(tracked or {})

        pre = _snapshot_processes()

        if pre is not None:
            for key, value in _descendants(process.pid, pre).items():
                tracked.setdefault(key, value)

        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
            )
            kill_ok = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            kill_ok = False
        tracked = dict(tracked or {})

        pre = _snapshot_processes()

        if pre is not None:
            for key, value in _descendants(process.pid, pre).items():
                tracked.setdefault(key, value)

        try:
            stdout, _ = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            state["cleanup"]["parent_reaped"] = False
            state["cleanup"]["tree_cleanup_confirmed"] = False

            return "", False

        parent_reaped = process.poll() is not None
        post = _snapshot_processes()

        if post is not None:
            for key, value in _descendants(process.pid, post).items():
                tracked.setdefault(key, value)

        waited, count, tracked = _wait_tree_gone(process.pid, tracked)
        state["cleanup"]["parent_reaped"] = parent_reaped
        state["cleanup"]["descendants_seen"] = count
        state["cleanup"]["tree_cleanup_confirmed"] = bool(kill_ok and parent_reaped and waited)

        return stdout, state["cleanup"]["tree_cleanup_confirmed"]


def _mark(state: dict, new_state: str, reason: dict | None = None) -> None:
    transition(state, new_state)

    if reason is not None:
        state["reason"] = reason


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

    try:
        rows = list(csv.reader(io.StringIO(result.stdout or "")))
    except csv.Error:
        return None

    for row in rows:
        if len(row) >= 2 and row[1].strip().isdigit() and int(row[1].strip()) == int(pid):
            return True

    return False


def _snapshot_processes() -> list[dict] | None:
    command = [
        "powershell", "-NoProfile", "-Command",
        "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,CreationDate | ConvertTo-Csv -NoTypeInformation",
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return None

    if result.returncode != 0:
        return None

    try:
        rows = list(csv.reader(io.StringIO(result.stdout or "")))
    except csv.Error:
        return None

    if not rows:
        return None

    header = [cell.strip() for cell in rows[0]]

    if not {"ProcessId", "ParentProcessId", "CreationDate"}.issubset(set(header)):
        return None

    processes = []

    for row in rows[1:]:
        if not any(cell.strip() for cell in row):
            continue

        if len(row) < 3:
            return None

        try:
            processes.append({
                "pid": int(row[0]),
                "parent": int(row[1]),
                "created": row[2].strip(),
            })
        except ValueError:
            return None

    if not processes:
        return None

    return processes


def _descendants(pid: int, snapshot: list[dict]) -> dict[int, str]:
    children: dict[int, list[dict]] = {}

    for item in snapshot:
        children.setdefault(item["parent"], []).append(item)

    found: dict[int, str] = {}
    stack = [int(pid)]

    while stack:
        current = stack.pop()

        for child in children.get(current, []):
            if child["pid"] in found:
                continue

            found[child["pid"]] = child["created"]
            stack.append(child["pid"])

    return found


def _still_same(pid: int, created: str, snapshot: list[dict]) -> bool:
    for item in snapshot:
        if item["pid"] != pid:
            continue

        return item["created"] == created

    return False


def _wait_tree_gone(pid: int, tracked: dict[int, str] | None = None, timeout: float = 3.0) -> tuple[bool, int, dict[int, str]]:
    """等待根 PID 的全部后代消失；tracked 为已跟踪集合，结束时返回并集。"""

    tracked = dict(tracked or {})
    deadline = time.monotonic() + timeout

    while True:
        snapshot = _snapshot_processes()

        if snapshot is None:
            return False, len(tracked), tracked

        for key, value in _descendants(pid, snapshot).items():
            tracked.setdefault(key, value)

        alive = [key for key, created in tracked.items() if _still_same(key, created, snapshot)]

        if not alive:
            return True, 0, tracked

        if time.monotonic() >= deadline:
            return False, len(alive), tracked

        time.sleep(0.2)


def _creation_iso(pid) -> str | None:
    created = _process_creation_time(pid)

    return created.astimezone(timezone.utc).isoformat() if created else None


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

    if creation != expected:
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
        alive = _pid_alive(state.get("pid"), state.get("process_created_at"))

        if alive is not True:
            _mark(state, "unknown")
            state["reason"] = {"code": "stale_running", "message": "进程不存在或无法确认身份"}
            workspace.write_state(state)

    return state
