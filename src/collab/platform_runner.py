"""编排 coding 步骤的平台执行通道（PERSONAL_AGENT_ORCH_PLATFORM=1 时启用）。

流程（对应 plans/personal-agent-orch-platform-wiring-plan-20261112.md 步骤 5）：

1. 建/复用沙箱（workspace_id=run_id、idempotency_key=orch-<run_id>-<attempt>，
   TTL 由 ops.submit_exec 自动续到 timeout+120 s）；
2. 拷入 task.md / plan.md / review.md / src/（只拷普通文件），
   记录 plan/review 的哈希；
3. submit_exec 跑 scripts/dsh_exec_wrapper.py（事件落盘，绕开输出上限），
   env 走白名单注入（不落库、不进 argv）；
4. 轮询 exec_detail（2 s 间隔，总 deadline=timeout+120 s）；
5. 校验产物流（拒绝越界与 reparse point）→ 拷回 run_dir/src；
6. plan/review 被改只记 plan_tampered 告警，不拦截（v1.1）；
7. 回收沙箱；失败时把 dsh.events.jsonl / dsh.err.txt 拷到 run_dir/_platform/。

失败映射（写进 RunResult.error）：code_timeout / code_cancelled /
sandbox_gone / artifact_escape，其余与 dsh_runner 对齐。
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import stat
import sys
import time

from pathlib import Path

from src.collab import dsh_runner
from src.collab.dsh_runner import RunResult
from src.platform import ops
from src.platform.env_injection import ENV_INJECTION_ALLOWLIST
from src.platform.ops import SandboxGone
from src.platform.paths import sandbox_root

logger = logging.getLogger("personal_agent.collab.platform_runner")

WRAPPER_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "dsh_exec_wrapper.py"
)

PROMPT_NAME = "prompt.txt"
EVENTS_NAME = "dsh.events.jsonl"
ERR_NAME = "dsh.err.txt"

# 拷入沙箱的宿主 run 目录内容（state.json 等平台无关文件不进沙箱）。
COPY_IN_FILES = ("task.md", "plan.md", "review.md")
COPY_IN_DIRS = ("src",)

POLL_INTERVAL_SECONDS = 2.0


class _SandboxGoneDuringRun(Exception):
    """轮询期间沙箱被回收（TTL / 手动 / 对账）。"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_reparse_point(path: Path) -> bool:
    """Windows 重解析点（junction / symlink）检测，不跟随链接。"""

    if path.is_symlink():
        return True

    try:
        attributes = os.stat(path, follow_symlinks=False).st_file_attributes
    except (OSError, AttributeError):
        return False

    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _copy_tree_in(source: Path, target: Path) -> None:
    for item in source.rglob("*"):
        if not item.is_file() or _is_reparse_point(item):
            continue

        destination = target / item.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, destination)


def _collect_artifacts(
    workspace_src: Path,
) -> tuple[list[Path], str | None]:
    """收集 sandbox workspace/src 下的普通文件；越界/链接即拒。"""

    if not workspace_src.is_dir():
        return [], None

    root = workspace_src.resolve()
    artifacts: list[Path] = []

    for item in workspace_src.rglob("*"):
        if _is_reparse_point(item):
            return [], f"artifact_escape: reparse point {item.name}"

        if not item.is_file():
            continue

        resolved = item.resolve()

        if root not in resolved.parents and resolved != root:
            return [], f"artifact_escape: {item.name} 解析到沙箱外"

        artifacts.append(item)

    return artifacts, None


def _build_injected_env(extra: dict[str, str] | None) -> dict[str, str]:
    """从宿主环境取白名单键，再叠加调用方显式给的值（如 DEEPSEEK_API_KEY）。"""

    injected: dict[str, str] = {}

    for key in ENV_INJECTION_ALLOWLIST:
        value = os.environ.get(key)

        if value:
            injected[key] = value

    injected["NO_BROWSER"] = "1"

    for key, value in (extra or {}).items():
        if key in ENV_INJECTION_ALLOWLIST and value:
            injected[key] = value

    return injected


def run_coding(
    prompt: str,
    run_dir: str | Path,
    *,
    run_id: str,
    timeout: float = 1800.0,
    dsh_path: str | Path = dsh_runner.DEFAULT_DSH,
    extra_env: dict[str, str] | None = None,
    attempt: int = 1,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    deadline_grace: float = 120.0,
) -> RunResult:
    """在平台沙箱里执行 coding 步骤；语义对齐 dsh_runner.run。"""

    started = time.monotonic()
    run_dir = Path(run_dir)
    sandbox_id = ""
    exec_id = ""

    def finish(result: RunResult) -> RunResult:
        result.elapsed_s = round(time.monotonic() - started, 1)
        result.sandbox_id = sandbox_id
        result.exec_id = exec_id
        return result

    # ---------- 1. 建沙箱 ----------
    try:
        sandbox = ops.create_sandbox(
            idempotency_key=f"orch-{run_id}-{attempt}",
            workspace_id=run_id,
            ttl_seconds=min(int(timeout) + 120, 86_400),
        )
    except Exception as exc:  # noqa: BLE001 - 平台错误统一成确定失败
        return finish(RunResult(error=f"sandbox_create_failed: {exc}"))

    sandbox_id = sandbox["id"]
    workspace = sandbox_root() / sandbox_id / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    try:
        # ---------- 2. 拷入 ----------
        for name in COPY_IN_FILES:
            source = run_dir / name

            if source.is_file():
                shutil.copy2(source, workspace / name)

        for name in COPY_IN_DIRS:
            source = run_dir / name

            if source.is_dir():
                _copy_tree_in(source, workspace / name)

        guard_hashes = {
            name: _sha256(workspace / name)
            for name in ("plan.md", "review.md")
            if (workspace / name).is_file()
        }

        (workspace / PROMPT_NAME).write_text(prompt, encoding="utf-8")

        # ---------- 3. 提交 exec ----------
        command = [
            sys.executable,
            str(WRAPPER_PATH),
            "--dsh", str(dsh_path),
            "--cwd", ".",
            "--stdin-file", PROMPT_NAME,
            "--stdout-file", EVENTS_NAME,
            "--stderr-file", ERR_NAME,
            "--",
            "headless", "--json",
            "--permission-mode", "workspace-write",
            "--add-dir", str(workspace),
            "-",
        ]

        row = ops.submit_exec(
            sandbox_id,
            command=command,
            timeout_ms=int(timeout * 1000),
            max_output_bytes=65_536,
            run_id=run_id,
            step_id="coding",
            env=_build_injected_env(extra_env),
            extend_ttl=True,
        )
        exec_id = row["id"]

        # ---------- 4. 轮询 ----------
        deadline = started + timeout + deadline_grace

        while True:
            try:
                detail = ops.exec_detail(exec_id)
            except SandboxGone as exc:
                raise _SandboxGoneDuringRun from exc

            state = detail.get("state")

            if state in ("succeeded", "failed", "cancelled", "timed_out"):
                break

            if time.monotonic() >= deadline:
                try:
                    ops.cancel_exec(exec_id)
                except Exception:  # noqa: BLE001 - 取消失败不改变超时结论
                    logger.exception("取消 exec 失败 id=%s", exec_id)

                raise _ExecPastDeadline

            time.sleep(poll_interval)

        exit_code = detail.get("exit_code")

        # ---------- 5. 解析事件 ----------
        events_path = workspace / EVENTS_NAME
        events_text = (
            events_path.read_text(encoding="utf-8", errors="replace")
            if events_path.is_file()
            else ""
        )
        parsed = dsh_runner.parse_events(events_text)
        reason = parsed["reason"]

        plan_tampered = any(
            (workspace / name).is_file() and _sha256(workspace / name) != digest
            for name, digest in guard_hashes.items()
        )

        # ---------- 6. 终态映射 ----------
        error = ""

        if state == "timed_out":
            error = "code_timeout"
        elif state == "cancelled":
            error = "code_cancelled"
        elif state == "failed" and not events_text:
            error = f"dsh_failed: {str(detail.get('stderr'))[-200:]}"
        elif exit_code != 0:
            error = f"exit={exit_code}"
        elif reason != "completed":
            error = f"reason={reason or 'missing'}"
        elif not parsed["text"].strip():
            error = "empty_final_text"

        result = RunResult(
            ok=not error,
            text=parsed["text"],
            reason=reason,
            exit_code=exit_code,
            usage=parsed["usage"],
            session_id=parsed["session_id"],
            events=parsed["events"],
            error=error,
        )
        result.platform_status = state
        result.plan_tampered = plan_tampered

        # ---------- 7. 产物校验与拷回 ----------
        if result.ok:
            artifacts, escape = _collect_artifacts(workspace / "src")

            if escape:
                result.ok = False
                result.error = escape
            else:
                target_src = run_dir / "src"

                if target_src.is_dir():
                    shutil.rmtree(target_src)

                target_src.mkdir(parents=True, exist_ok=True)

                for item in artifacts:
                    destination = target_src / item.relative_to(workspace / "src")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, destination)

        return finish(result)

    except _SandboxGoneDuringRun:
        result = RunResult(error="sandbox_gone")
        result.platform_status = "reclaimed"
        return finish(result)
    except _ExecPastDeadline:
        result = RunResult(error="code_timeout")
        result.platform_status = "deadline_exceeded"
        return finish(result)
    finally:
        # ---------- 留证与回收 ----------
        evidence = run_dir / "_platform"

        for name in (EVENTS_NAME, ERR_NAME):
            source = workspace / name

            if source.is_file():
                evidence.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, evidence / name)

        if sandbox_id:
            try:
                ops.destroy_sandbox(sandbox_id)
            except Exception:  # noqa: BLE001 - 回收失败由 reaper 兜底
                logger.exception("回收沙箱失败 id=%s", sandbox_id)


class _ExecPastDeadline(Exception):
    """宿主侧轮询超过 timeout+120 s 还没等到终态。"""
