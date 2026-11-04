"""平台执行层（D11 最小版）：一次受控执行的生命周期。

链路：助手 → 平台 exec（本模块）→ tier1 runner 子进程 → 进程树清理 → 终态。

这里的每一条约束都来自 ``docs/platform-contract.md``：

- **先清树，后落终态。** 任何终态迁移之前都要等 job 的活动进程数归零；
  清理失败落 failed / ``RUNNER_CLEANUP_FAILED``，绝不伪造 cancelled。
- **一个输出预算。** stdout 与 stderr 合计受 ``max_output_bytes`` 约束，
  超限立刻终止整棵树，只保留上限内的前缀并标 ``truncated``。
- **不继承宿主环境。** 子进程只拿到操作系统运行所需的白名单变量；
  数据目录由平台显式授予，凭据、``.env``、``PYTHONPATH`` 一律不传。
- **取消要送到进程里。** ``CancellationToken`` 的回调直接终止 job，
  不是只改一行数据库状态。

尚未实现（属于 D15—D20，不在 D11 范围内）：HTTP 控制面、幂等 create、
节点租约、TTL / reaper、完整 sandbox 状态机。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from src import config
from src.core.reminder_identity import ReminderDeliveryUnavailable
from src.core.run_control import CancellationToken
from src.core.tool_registry import ToolApprovalRequired, ToolDenied
from src.platform.process_tree import (
    CLEANUP_JOB_OBJECT,
    ProcessTree,
    ProcessTreeError,
)
from src.platform.trace_context import new_traceparent

logger = logging.getLogger("personal_agent.platform")

EXEC_SUCCEEDED = "succeeded"
EXEC_FAILED = "failed"
EXEC_CANCELLED = "cancelled"
EXEC_TIMED_OUT = "timed_out"

ERROR_EXEC_FAILED = "EXEC_FAILED"
ERROR_TIMED_OUT = "EXEC_TIMED_OUT"
ERROR_CANCELLED = "EXEC_CANCELLED"
ERROR_OUTPUT_LIMIT = "OUTPUT_LIMIT_EXCEEDED"
ERROR_CLEANUP = "RUNNER_CLEANUP_FAILED"
ERROR_RUNNER = "RUNNER_FAILED"
ERROR_TOOL = "TOOL_FAILED"

DEFAULT_TIMEOUT_MS = 30_000
MAX_TIMEOUT_MS = 300_000
DEFAULT_MAX_OUTPUT_BYTES = 65_536
MAX_OUTPUT_BYTES = 1_048_576

CLEANUP_TIMEOUT_SECONDS = 5.0

REASON_CANCEL = "cancel"
REASON_TIMEOUT = "timeout"
REASON_OUTPUT = "output_limit"

# 只传给 runner 的操作系统级变量；存在才传。
ENV_ALLOWLIST = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
)

TIER1_ENV = "PERSONAL_AGENT_TIER1"

RUNNER_PATH = Path(__file__).resolve().with_name("tier1_runner.py")

ROOT_ENV = "PERSONAL_AGENT_SANDBOX_ROOT"
DEFAULT_ROOT_NAME = "personal-agent-tier1"

# 平台自己生成的沙箱 ID；复用沙箱时必须是这个形状，不能让调用方传路径进来。
SANDBOX_ID_PATTERN = re.compile(r"sbx_[0-9a-f]{32}")

TOOL_ERROR_TYPES: dict[str, type[BaseException]] = {
    "ValueError": ValueError,
    "TypeError": TypeError,
    "KeyError": KeyError,
    "PermissionError": PermissionError,
    "ToolApprovalRequired": ToolApprovalRequired,
    "ToolDenied": ToolDenied,
    "ReminderDeliveryUnavailable": ReminderDeliveryUnavailable,
}


class PlatformExecutionError(RuntimeError):
    """平台层执行失败（超时、输出超限、runner 故障等，不是工具自身报错）。"""

    def __init__(self, message: str, *, status: str, error_code: str) -> None:
        super().__init__(message)
        self.status = status
        self.error_code = error_code


class PlatformToolError(RuntimeError):
    """runner 报告了一个平台不认识的工具异常类型。"""


@dataclass(frozen=True)
class ExecOutcome:
    """一次受控执行的可核验结果。"""

    sandbox_id: str
    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    truncated: bool
    result: dict[str, Any] | None = None
    tool_error: dict[str, Any] | None = None
    error_code: str | None = None
    cleanup: str = CLEANUP_JOB_OBJECT
    duration_ms: int = 0
    cancel_requested_at: str | None = None
    traceparent: str | None = None


def clamp(value: int | None, default: int, maximum: int) -> int:
    if value is None:
        return default
    if type(value) is not int or value <= 0:
        raise ValueError("执行限制必须是正整数")
    return min(value, maximum)


def default_root() -> Path:
    """与 sandbox_service 共用同一个根目录（见 src/platform/paths.py）。"""

    from src.platform.paths import sandbox_root

    return sandbox_root()


def platform_environment() -> dict[str, str]:
    """构造子进程环境：白名单 + 一个「运行在 tier1」标记。"""

    environment: dict[str, str] = {}

    for name in ENV_ALLOWLIST:
        value = os.environ.get(name)
        if value:
            environment[name] = value

    environment[TIER1_ENV] = "1"

    # 固定 runner 自身的输出编码：否则中文 Windows 会按 cp936 写管道，
    # 宿主按 UTF-8 解码就得到一串替换字符。用户命令自己的编码不在
    # D11 范围内（D12 的 shell / python 工具再定策略）。
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"

    return environment


def raise_tool_error(tool_error: dict[str, Any] | None) -> None:
    """把 runner 上报的工具异常还原成同类型异常，保持既有错误分支不变。"""

    if not tool_error:
        return

    name = str(tool_error.get("error_type") or "")
    message = str(tool_error.get("message") or "工具执行失败")

    exception_type = TOOL_ERROR_TYPES.get(name)

    if exception_type is None:
        raise PlatformToolError(f"{name or '未知异常'}：{message}")

    raise exception_type(message)


class _RunState:
    """终止原因的「先到先得」仲裁。

    取消、超时、输出超限可能同时发生；第一个写下的原因决定终态。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reason: str | None = None
        self.requested_at: str | None = None

    def request(self, reason: str, timestamp: str | None = None) -> bool:
        with self._lock:
            if self._reason is not None:
                return False
            self._reason = reason
            self.requested_at = timestamp or _utc_now()
            return True

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class _OutputCollector:
    """stdout 与 stderr 合计一个字节预算。"""

    def __init__(self, limit: int) -> None:
        self._lock = threading.Lock()
        self._limit = limit
        self._used = 0
        self._chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
        self.truncated = False

    def append(self, source: str, data: bytes) -> bool:
        """写入数据；返回 True 表示已超过上限。"""

        with self._lock:
            room = self._limit - self._used

            if room <= 0:
                self.truncated = True
                return True

            kept = data[:room]
            self._chunks[source].append(kept)
            self._used += len(kept)

            if len(data) > room:
                self.truncated = True
                return True

            return False

    def text(self, source: str) -> str:
        with self._lock:
            return b"".join(self._chunks[source]).decode("utf-8", "replace")


class Tier1Executor:
    """创建独立工作区、启动 runner、执行一条任务并保证清树。"""

    def __init__(self, root: str | Path | None = None, python: str | None = None):
        self.root = Path(root) if root is not None else default_root()
        self.python = python or sys.executable

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        identity: dict[str, str] | None,
        cancel_token: CancellationToken | None = None,
        timeout_ms: int | None = None,
        max_output_bytes: int | None = None,
        call_id: str | None = None,
        sandbox_id: str | None = None,
        on_started: Callable[[str], None] | None = None,
        on_cancel_requested: Callable[[str], None] | None = None,
        traceparent: str | None = None,
    ) -> ExecOutcome:
        spec: dict[str, Any] = {
            "tool": tool_name,
            "arguments": arguments,
            "identity": identity,
            "call_id": call_id,
        }

        return self._run(
            kind="tool",
            spec=spec,
            data_dir=self._data_dir(),
            sandbox_id=sandbox_id,
            cancel_token=cancel_token,
            timeout_ms=timeout_ms,
            max_output_bytes=max_output_bytes,
            on_started=on_started,
            on_cancel_requested=on_cancel_requested,
            traceparent=traceparent,
        )

    def execute_command(
        self,
        argv: list[str],
        *,
        cancel_token: CancellationToken | None = None,
        timeout_ms: int | None = None,
        max_output_bytes: int | None = None,
        sandbox_id: str | None = None,
        on_started: Callable[[str], None] | None = None,
        on_cancel_requested: Callable[[str], None] | None = None,
        traceparent: str | None = None,
    ) -> ExecOutcome:
        return self._run(
            kind="command",
            spec={"argv": list(argv)},
            data_dir=None,
            sandbox_id=sandbox_id,
            cancel_token=cancel_token,
            timeout_ms=timeout_ms,
            max_output_bytes=max_output_bytes,
            on_started=on_started,
            on_cancel_requested=on_cancel_requested,
            traceparent=traceparent,
        )

    def open_sandbox(self) -> str:
        """创建一个可跨多次 exec 复用的沙箱工作区（D12 文件工具需要）。"""

        sandbox_id = f"sbx_{uuid.uuid4().hex}"
        (self.root / sandbox_id / "workspace").mkdir(
            parents=True, exist_ok=False
        )
        return sandbox_id

    def destroy(self, sandbox_id: str) -> None:
        """回收工作区。D11 只做本地目录清理；路径由平台自己生成。"""

        if not sandbox_id.startswith("sbx_"):
            raise ValueError("非法 sandbox_id")

        target = self.root / sandbox_id
        shutil.rmtree(target, ignore_errors=True)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    @staticmethod
    def _data_dir() -> str:
        # 调用时读取，测试替换过 src.config.paths 也能拿到隔离目录。
        return str(Path(config.paths.reminders_db).parent)

    def _run(
        self,
        *,
        kind: str,
        spec: dict[str, Any],
        data_dir: str | None,
        cancel_token: CancellationToken | None,
        timeout_ms: int | None,
        max_output_bytes: int | None,
        on_started: Callable[[str], None] | None,
        on_cancel_requested: Callable[[str], None] | None,
        sandbox_id: str | None = None,
        traceparent: str | None = None,
    ) -> ExecOutcome:
        timeout = clamp(timeout_ms, DEFAULT_TIMEOUT_MS, MAX_TIMEOUT_MS)
        output_limit = clamp(
            max_output_bytes, DEFAULT_MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES
        )

        if sandbox_id is not None and SANDBOX_ID_PATTERN.fullmatch(sandbox_id) is None:
            raise ValueError("非法 sandbox_id")

        sandbox_id = sandbox_id or f"sbx_{uuid.uuid4().hex}"
        started = time.monotonic()
        traceparent = traceparent or new_traceparent()

        if cancel_token is not None and cancel_token.cancelled:
            # 已经取消：不启动进程，直接给确定终态。
            return ExecOutcome(
                sandbox_id=sandbox_id,
                status=EXEC_CANCELLED,
                exit_code=None,
                stdout="",
                stderr="",
                truncated=False,
                error_code=ERROR_CANCELLED,
                cleanup=CLEANUP_JOB_OBJECT,
                duration_ms=0,
                traceparent=traceparent,
            )

        sandbox_dir = self.root / sandbox_id
        workspace = sandbox_dir / "workspace"
        # 复用已存在的沙箱；新建时等价于 mkdir。
        workspace.mkdir(parents=True, exist_ok=True)
        result_path = sandbox_dir / "result.json"

        payload = {
            "schema_version": 1,
            "kind": kind,
            "traceparent": traceparent,
            **spec,
        }

        argv = [
            self.python,
            str(RUNNER_PATH),
            "--workspace",
            str(workspace),
            "--result",
            str(result_path),
            "--kind",
            kind,
        ]

        if data_dir is not None:
            argv.extend(["--data-dir", data_dir])

        state = _RunState()
        collector = _OutputCollector(output_limit)
        tree: ProcessTree | None = None
        popen: subprocess.Popen | None = None
        readers: list[threading.Thread] = []
        unregister: Callable[[], None] = lambda: None
        cleanup_mode = CLEANUP_JOB_OBJECT

        creationflags = 0

        if os.name == "nt":
            creationflags = (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )

        def terminate_tree() -> None:
            if tree is None:
                return
            try:
                tree.terminate()
            except ProcessTreeError:
                logger.exception("终止 tier1 进程树失败 sandbox_id=%s", sandbox_id)

        try:
            popen = subprocess.Popen(
                argv,
                cwd=str(workspace),
                env=platform_environment(),
                # stdin 是「放行闸门」：宿主分配完 job 才写入任务说明。
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=creationflags,
            )

            tree = ProcessTree(popen)
            cleanup_mode = tree.mode

            if on_started is not None:
                on_started(sandbox_id)

            try:
                assert popen.stdin is not None
                popen.stdin.write(
                    json.dumps(payload, ensure_ascii=False).encode("utf-8")
                )
                popen.stdin.close()
            except (BrokenPipeError, OSError):
                logger.warning("runner 在读取任务说明前退出 sandbox_id=%s", sandbox_id)

            def pump(stream, source: str) -> None:
                while True:
                    try:
                        data = stream.read(4096)
                    except (OSError, ValueError):
                        return
                    if not data:
                        return
                    if collector.append(source, data):
                        if state.request(REASON_OUTPUT):
                            terminate_tree()
                        return

            if popen.stdout is not None:
                reader = threading.Thread(
                    target=pump, args=(popen.stdout, "stdout"), daemon=True
                )
                reader.start()
                readers.append(reader)

            if popen.stderr is not None:
                reader = threading.Thread(
                    target=pump, args=(popen.stderr, "stderr"), daemon=True
                )
                reader.start()
                readers.append(reader)

            if cancel_token is not None:

                def on_cancel() -> None:
                    if not state.request(REASON_CANCEL):
                        return
                    if on_cancel_requested is not None:
                        try:
                            on_cancel_requested(state.requested_at or _utc_now())
                        except Exception:  # noqa: BLE001 - 记录失败不能吃掉取消
                            logger.exception(
                                "取消事件写入失败 sandbox_id=%s", sandbox_id
                            )
                    terminate_tree()

                unregister = cancel_token.register_canceller(on_cancel)

            deadline = started + timeout / 1000.0

            while True:
                if popen.poll() is not None:
                    break
                if time.monotonic() >= deadline:
                    if state.request(REASON_TIMEOUT):
                        terminate_tree()
                time.sleep(0.01)

            popen.wait(timeout=timeout / 1000.0 + CLEANUP_TIMEOUT_SECONDS)

            for reader in readers:
                reader.join(timeout=1.0)

            # 正常退出也可能留下后台子进程；终态之前必须把它们一并清掉。
            leftover = tree.owned_process_count

            if leftover is None or leftover > 0 or state.reason is not None:
                terminate_tree()

            cleaned = tree.wait_empty(CLEANUP_TIMEOUT_SECONDS)

            if not cleaned:
                terminate_tree()
                cleaned = tree.wait_empty(1.0)
        finally:
            unregister()

            if popen is not None:
                if popen.poll() is None:
                    terminate_tree()
                    try:
                        popen.wait(timeout=CLEANUP_TIMEOUT_SECONDS)
                    except subprocess.TimeoutExpired:
                        try:
                            popen.kill()
                        except OSError:
                            pass

                for stream in (popen.stdout, popen.stderr):
                    try:
                        if stream is not None:
                            stream.close()
                    except OSError:
                        pass

            if tree is not None:
                tree.close()

        duration_ms = int((time.monotonic() - started) * 1000)
        exit_code = popen.returncode

        result_payload: dict[str, Any] | None = None
        tool_error: dict[str, Any] | None = None

        if kind == "tool" and result_path.is_file():
            try:
                result_payload = json.loads(
                    result_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                result_payload = None

        if result_payload is not None and result_payload.get("ok") is False:
            tool_error = result_payload

        status, error_code = _classify(
            kind=kind,
            reason=state.reason,
            cleaned=cleaned,
            exit_code=exit_code,
            truncated=collector.truncated,
            tool_error=tool_error,
        )

        return ExecOutcome(
            sandbox_id=sandbox_id,
            status=status,
            exit_code=exit_code,
            stdout=collector.text("stdout"),
            stderr=collector.text("stderr"),
            truncated=collector.truncated,
            result=(
                result_payload.get("result")
                if result_payload is not None and result_payload.get("ok") is True
                else None
            ),
            tool_error=tool_error,
            error_code=error_code,
            cleanup=cleanup_mode,
            duration_ms=duration_ms,
            cancel_requested_at=state.requested_at,
            traceparent=traceparent,
        )


def _classify(
    *,
    kind: str,
    reason: str | None,
    cleaned: bool,
    exit_code: int | None,
    truncated: bool,
    tool_error: dict[str, Any] | None,
) -> tuple[str, str | None]:
    """按契约把执行结果映射成 (status, error_code)。"""

    if not cleaned:
        return EXEC_FAILED, ERROR_CLEANUP

    if reason == REASON_CANCEL:
        return EXEC_CANCELLED, ERROR_CANCELLED

    if reason == REASON_TIMEOUT:
        return EXEC_TIMED_OUT, ERROR_TIMED_OUT

    if reason == REASON_OUTPUT or truncated:
        return EXEC_FAILED, ERROR_OUTPUT_LIMIT

    if kind == "tool":
        if tool_error is not None:
            return EXEC_FAILED, ERROR_TOOL
        if exit_code == 0:
            return EXEC_SUCCEEDED, None
        return EXEC_FAILED, ERROR_RUNNER

    if exit_code == 0:
        return EXEC_SUCCEEDED, None

    return EXEC_FAILED, ERROR_EXEC_FAILED


def default_executor() -> Tier1Executor:
    """按当前配置创建执行器；调用方负责 destroy。"""

    return Tier1Executor()
