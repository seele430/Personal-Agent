"""PA 主 Agent 编排：千问写计划 / 改计划 / 终验收，DSH 评审 / 写代码。

边界（写死）：

- 只认文件里的 `VERDICT:` 行与产物存在性，**不认子 Agent 自评**；
- 缺文件 / 无匹配 / 子进程失败一律 `failed` → `stopped`（fail-closed）；
- 返工上限只覆盖计划阶段（`revising ⇄ reviewing ≤ max_revisions`）；
- 验收 `FAIL` 直接 `stopped`，不回编码；
- 单 run 串行，状态落 `data/runs/<run_id>/state.json`。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import logging

from src.collab import dsh_runner
from src.collab import platform_runner
from src.collab.dsh_runner import ENV_WHITELIST
from src.platform.audit import AuditLog

logger = logging.getLogger("personal_agent.collab.orchestrator")


DEFAULT_QWEN = r"C:\Users\123\AppData\Roaming\npm\qwen.cmd"
VERDICT_RE = re.compile(r"^VERDICT:\s*(PASS|FAIL)\s*$", re.IGNORECASE | re.MULTILINE)
MAX_REVISIONS = 2
DEFAULT_TIMEOUT = 600.0
STEP_TIMEOUTS = {
    "planning": 600.0,
    "revising": 600.0,
    "reviewing": 600.0,
    "coding": 1800.0,
    "verifying": 900.0,
}
MIN_PLAN_CHARS = 80
RUNS_DIRNAME = "runs"
PLATFORM_FLAG_ENV = "PERSONAL_AGENT_ORCH_PLATFORM"
# 平台通道的 coding 失败码：直接作为 stop_reason，其余仍归 code_failed。
CODE_FAILURE_REASONS = frozenset(
    {"code_timeout", "code_cancelled", "sandbox_gone", "artifact_escape"}
)


def _platform_enabled() -> bool:
    """coding 步骤走平台沙箱执行；默认关，行为与旧路径完全一致。"""

    return os.getenv(PLATFORM_FLAG_ENV, "").strip() == "1"


class OrchestratorError(RuntimeError):
    """编排参数/状态不合法。"""


def data_root() -> Path:
    raw = (os.getenv("PERSONAL_AGENT_DATA_DIR") or "").strip()

    if raw:
        return Path(raw)

    return Path(__file__).resolve().parents[2] / "data"


def read_verdict(path: str | Path) -> str | None:
    """fail-closed：文件缺失 / 无匹配 → None（调用方必须当失败处理）。"""

    path = Path(path)

    if not path.is_file():
        return None

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    matches = VERDICT_RE.findall(text)

    return matches[-1].upper() if matches else None


def _dsh_env() -> dict:
    """DSH headless 的凭据：优先 DEEPSEEK_API_KEY，退回 PA 的 OPENAI_API_KEY。"""

    key = (os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()

    return {"DEEPSEEK_API_KEY": key} if key else {}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".orchestrator-", suffix=".tmp", dir=str(path.parent)
    )

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _kill_tree(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _resolve_qwen_cli() -> list[str] | None:
    """直调 qwen-code 自更新缓存里的当前版 cli.js，返回 argv 前缀。

    背景：qwen.cmd → cli-entry.js（0.27.0 引导层）→ 自更新转发到缓存版
    cli.js 的链路会把位置参数吞掉（模型实际收到 cli.js 的路径而不是
    提示词）；直调 active.json 指向的当前版 cli.js 则参数完好。
    本机没有独立 Node，npm 目录里的 node.exe 是指向 Kimi.exe（Electron）
    的硬链接，配合 env 里的 ELECTRON_RUN_AS_NODE=1 以纯 Node 模式运行。
    解析失败返回 None，调用方回退到 qwen.cmd。
    """

    try:
        node_exe = Path(os.environ.get("APPDATA", "")) / "npm" / "node.exe"
        if not node_exe.is_file():
            return None

        updates = Path.home() / ".qwen" / "updates" / "npm"
        candidates = sorted(
            updates.glob("*/active.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for active_file in candidates:
            try:
                version = json.loads(active_file.read_text(encoding="utf-8"))["version"]
            except (OSError, ValueError, KeyError):
                continue
            cli_js = (
                active_file.parent / "versions" / version
                / "node_modules" / "@qwen-code" / "qwen-code" / "cli.js"
            )
            if cli_js.is_file():
                return [str(node_exe), str(cli_js)]
        return None
    except OSError:
        return None


class QwenResult:
    def __init__(self, ok, text="", exit_code=None, elapsed_s=0.0, error=""):
        self.ok = ok
        self.text = text
        self.exit_code = exit_code
        self.elapsed_s = elapsed_s
        self.error = error


def call_qwen(
    prompt: str,
    workspace: str | Path,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    qwen_cmd: str = DEFAULT_QWEN,
    extra_env: dict | None = None,
) -> QwenResult:
    """一次非交互调用（plan 模式：只读分析，不改文件）。"""

    text = (prompt or "").strip()

    if not text:
        return QwenResult(False, error="empty_prompt")

    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    argv_prefix = _resolve_qwen_cli() or [qwen_cmd]
    command = argv_prefix + [
        # qwen-code 0.27.x 的参数解析有两个坑：
        # 1) --prompt/-p 与 "--" 分隔符都会把提示词吞掉（模型改收到 cli.js 路径）；
        # 2) --include-directories 是可变参数，会把跟在后面的位置参数（提示词）
        #    一起吞掉。因此提示词作为纯位置参数放在最前面。
        text,
        "--approval-mode", "plan",
        "--output-format", "text",
        "--include-directories", str(workspace),
    ]

    source = os.environ
    env = {key: source[key] for key in ENV_WHITELIST if key in source}
    env["NO_BROWSER"] = "1"
    env["NO_PROXY"] = env.get("NO_PROXY", "*")
    # npm 全局目录里的 node.exe 是指向 Kimi.exe（Electron）的硬链接，
    # 机器上没有独立 Node 运行时；ELECTRON_RUN_AS_NODE=1 让它以纯 Node 模式运行。
    # 若将来安装了独立 Node，此变量无害。
    env["ELECTRON_RUN_AS_NODE"] = env.get("ELECTRON_RUN_AS_NODE", "1")

    for key, value in (extra_env or {}).items():
        if value:
            env[key] = value

    started = time.monotonic()

    try:
        process = subprocess.Popen(
            command,
            cwd=str(workspace),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as error:
        return QwenResult(False, error=f"spawn_failed: {error}")

    try:
        stdout, stderr = process.communicate(timeout=timeout)
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

        return QwenResult(False, elapsed_s=round(time.monotonic() - started, 1),
                          error=f"timeout>{timeout}s")

    elapsed = round(time.monotonic() - started, 1)
    answer = (stdout or "").strip()

    if process.returncode != 0:
        return QwenResult(False, answer, process.returncode, elapsed,
                          f"exit={process.returncode}: {(stderr or '')[-200:]}")

    if not answer:
        return QwenResult(False, "", process.returncode, elapsed, "empty_output")

    return QwenResult(True, answer, process.returncode, elapsed)


class Orchestrator:
    def __init__(
        self,
        root: str | Path | None = None,
        *,
        qwen: Callable[..., QwenResult] = call_qwen,
        dsh: Callable[..., dsh_runner.RunResult] = dsh_runner.run,
        platform_run: Callable[..., dsh_runner.RunResult] = platform_runner.run_coding,
        notifier: Callable[[str], None] | None = None,
        timeout: float | None = None,
        step_timeouts: dict | None = None,
        max_revisions: int = MAX_REVISIONS,
    ):
        self.root = Path(root) if root else data_root() / RUNS_DIRNAME
        self.qwen = qwen
        self.dsh = dsh
        self.platform_run = platform_run
        self.notifier = notifier
        self.max_revisions = max_revisions

        self.step_timeouts = dict(STEP_TIMEOUTS)

        if step_timeouts:
            self.step_timeouts.update(step_timeouts)

        if timeout is not None:
            # 显式传 timeout 时统一覆盖所有步骤（测试/临时调试用）
            self.step_timeouts = {name: float(timeout) for name in STEP_TIMEOUTS}

    def _timeout_for(self, step: str) -> float:
        return float(self.step_timeouts.get(step, DEFAULT_TIMEOUT))

    # ---------- 状态持久化 ----------

    def run_dir(self, run_id: str) -> Path:
        return self.root / run_id

    def create(self, task: str) -> dict:
        task = (task or "").strip()

        if not task:
            raise OrchestratorError("任务不能为空")

        run_id = "run-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4_short()
        directory = self.run_dir(run_id)
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "src").mkdir()
        _write_atomic(directory / "task.md", f"# 任务\n\n{task}\n")

        state = {
            "schema_version": 1,
            "run_id": run_id,
            "task": task,
            "state": "created",
            "step": "",
            "revisions": 0,
            "max_revisions": self.max_revisions,
            "created_at": _now(),
            "updated_at": _now(),
            "history": [],
            "steps": {},
        }
        self.save(state)

        return state

    def load(self, run_id: str) -> dict:
        path = self.run_dir(run_id) / "state.json"

        if not path.is_file():
            raise OrchestratorError(f"run 不存在：{run_id}")

        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, state: dict) -> None:
        state["updated_at"] = _now()
        _write_atomic(
            self.run_dir(state["run_id"]) / "state.json",
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        )

    def _history(self, state: dict, event: str, **fields) -> None:
        entry = {"at": _now(), "event": event}
        entry.update(fields)
        state.setdefault("history", []).append(entry)

    def _notify(self, text: str) -> None:
        if self.notifier is None:
            return

        try:
            self.notifier(text)
        except Exception:  # noqa: BLE001 - 通知失败不影响流程
            pass

    def _audit(
        self,
        event: str,
        state: dict,
        *,
        outcome: str,
        code: str | None = None,
        sandbox_id: str | None = None,
        exec_id: str | None = None,
    ) -> None:
        """编排关键节点落平台审计；审计是旁路，失败不影响流程。"""

        try:
            AuditLog().record(
                event,
                outcome=outcome,
                code=code,
                actor="orchestrator",
                sandbox_id=sandbox_id,
                exec_id=exec_id,
                detail={"run_id": state["run_id"], "step": state.get("step")},
            )
        except Exception:  # noqa: BLE001 - 与 ops 一致：审计失败不挡主流程
            logger.exception("审计写入失败 event=%s", event)

    # ---------- 单步 ----------

    def _step_failed(self, state: dict, step: str, error: str) -> None:
        # update 而不是覆盖：保留 coding 已写入的 platform 等诊断字段。
        entry = state["steps"].setdefault(step, {})
        entry.update({"ok": False, "error": error, "at": _now()})
        self._history(state, "step_failed", step=step, error=error)

    def _plan(self, state: dict, *, revise: bool = False) -> bool:
        directory = self.run_dir(state["run_id"])
        step = "revising" if revise else "planning"
        state["step"] = step
        self.save(state)
        self._notify(f"[编排 {state['run_id']}] {step} 开始")

        if revise:
            review = (directory / "review.md").read_text(encoding="utf-8") if (directory / "review.md").is_file() else ""
            prompt = (
                "你要修订一份计划。原计划见 plan.md，评审意见见下方。"
                "只输出修订后的完整计划正文，不要解释。\n\n评审意见：\n" + review[-4000:]
            )
        else:
            prompt = (
                "你是计划者。为下面任务写一份可执行的计划：步骤、涉及文件、验收方式。"
                "只输出计划正文，不要解释。\n\n任务：\n" + state["task"]
            )

        result = self.qwen(prompt, directory, timeout=self._timeout_for(state["step"]))
        state["steps"][step] = {
            "ok": bool(result.ok), "elapsed_s": result.elapsed_s,
            "exit_code": result.exit_code, "chars": len(result.text or ""),
            "at": _now(),
        }

        if not result.ok:
            self._step_failed(state, step, result.error or "qwen_failed")
            self.save(state)

            return False

        if len(result.text.strip()) < MIN_PLAN_CHARS:
            self._step_failed(state, step, "plan_too_short")
            self.save(state)

            return False

        _write_atomic(directory / "plan.md", result.text.strip() + "\n")
        self._history(state, "plan_written" if not revise else "plan_revised",
                      chars=len(result.text), revision=state["revisions"])
        self.save(state)
        self._notify(f"[编排 {state['run_id']}] {step} 完成（{len(result.text)} 字）")

        return True

    def _review(self, state: dict) -> str | None:
        directory = self.run_dir(state["run_id"])
        state["step"] = "reviewing"
        self.save(state)
        self._notify(f"[编排 {state['run_id']}] reviewing 开始")

        prompt = (
            "读取当前目录的 plan.md，评审这份计划是否可行、有无遗漏。"
            "把评审意见写入 review.md，**文件最后一行必须是 `VERDICT: PASS` 或 `VERDICT: FAIL`**。"
            "除这个文件外不要改其他文件。"
        )
        result = self.dsh(prompt, directory, timeout=self._timeout_for(state["step"]), extra_env=_dsh_env())
        state["steps"]["reviewing"] = {
            "ok": bool(result.ok), "elapsed_s": result.elapsed_s,
            "exit_code": result.exit_code, "events": result.events,
            "error": result.error, "at": _now(),
        }

        if not result.ok:
            self._step_failed(state, "reviewing", result.error or "dsh_failed")
            self.save(state)

            return None

        verdict = read_verdict(directory / "review.md")
        state["steps"]["reviewing"]["verdict"] = verdict
        self._history(state, "review_verdict", verdict=verdict)
        self.save(state)

        if verdict is None:
            return None

        self._notify(f"[编排 {state['run_id']}] 评审结论：{verdict}")

        return verdict

    def _code(self, state: dict) -> bool:
        directory = self.run_dir(state["run_id"])
        state["step"] = "coding"
        self.save(state)
        self._notify(f"[编排 {state['run_id']}] coding 开始")

        prompt = (
            "按当前目录的 plan.md 在 src/ 下实现任务。改完不要动 plan.md 与 review.md。"
            "最后用 3 行以内说明你产出了哪些文件。"
        )

        if _platform_enabled():
            self._audit("orchestrator.coding_started", state, outcome="ok")

            result = self.platform_run(
                prompt,
                directory,
                run_id=state["run_id"],
                timeout=self._timeout_for(state["step"]),
                extra_env=_dsh_env(),
                attempt=state["revisions"] + 1,
            )

            self._audit(
                "orchestrator.coding_finished",
                state,
                outcome="ok" if result.ok else "failed",
                code=result.error or None,
                sandbox_id=result.sandbox_id or None,
                exec_id=result.exec_id or None,
            )
        else:
            result = self.dsh(prompt, directory, timeout=self._timeout_for(state["step"]), extra_env=_dsh_env())

        state["steps"]["coding"] = {
            "ok": bool(result.ok), "elapsed_s": result.elapsed_s,
            "exit_code": result.exit_code, "summary": (result.text or "")[-300:],
            "error": result.error, "at": _now(),
        }

        if result.sandbox_id or result.exec_id:
            state["steps"]["coding"]["platform"] = {
                "sandbox_id": result.sandbox_id,
                "exec_id": result.exec_id,
                "status": result.platform_status,
                "elapsed_s": result.elapsed_s,
            }

        if result.plan_tampered:
            # v1.1：只告警不拦截，证据留在 run_dir/_platform/
            state["steps"]["coding"]["plan_tampered_warning"] = True
            self._history(state, "plan_tampered_warning", step="coding")

        artifacts = sorted(
            str(path.relative_to(directory))
            for path in (directory / "src").rglob("*")
            if path.is_file()
        )
        state["steps"]["coding"]["artifacts"] = artifacts
        state["artifacts"] = artifacts

        if not result.ok:
            self._step_failed(state, "coding", result.error or "dsh_failed")
            self.save(state)

            return False

        if not artifacts:
            self._step_failed(state, "coding", "no_artifacts")
            self.save(state)

            return False

        self._history(state, "code_written", artifacts=len(artifacts))
        self.save(state)
        self._notify(f"[编排 {state['run_id']}] coding 完成，产物 {len(artifacts)} 个")

        return True

    def _verify(self, state: dict) -> str | None:
        directory = self.run_dir(state["run_id"])
        state["step"] = "verifying"
        self.save(state)
        self._notify(f"[编排 {state['run_id']}] verifying 开始")

        prompt = (
            "你是验收者。读取 plan.md 与 src/ 下的实现，判断是否满足计划。"
            "目录下的 _platform/ 只是平台运行证据，与验收无关，不要读取或评价它。"
            "你处于只读模式，不要写任何文件，只在回复里输出验收理由；"
            "回复的最后一行必须是 `VERDICT: PASS` 或 `VERDICT: FAIL`。"
        )
        result = self.qwen(prompt, directory, timeout=self._timeout_for(state["step"]))
        state["steps"]["verifying"] = {
            "ok": bool(result.ok), "elapsed_s": result.elapsed_s,
            "exit_code": result.exit_code, "chars": len(result.text or ""),
            "error": result.error, "at": _now(),
        }

        if not result.ok:
            self._step_failed(state, "verifying", result.error or "qwen_failed")
            self.save(state)
            self._notify(f"[编排 {state['run_id']}] 验收失败：{result.error}")

            return None

        _write_atomic(directory / "verdict.md", result.text.strip() + "\n")
        verdict = read_verdict(directory / "verdict.md")
        state["steps"]["verifying"]["verdict"] = verdict
        self._history(state, "verify_verdict", verdict=verdict)
        self.save(state)
        self._notify(f"[编排 {state['run_id']}] 验收结论：{verdict or '缺失'}")

        return verdict

    # ---------- 主流程 ----------

    def run(self, run_id: str) -> dict:
        state = self.load(run_id)

        if state.get("state") in ("done", "stopped"):
            return state

        state["state"] = "running"
        self.save(state)
        self._notify(f"[编排 {run_id}] 开工：{state['task'][:60]}")

        try:
            if not self._plan(state):
                return self._stop(state, "plan_failed")

            while True:
                verdict = self._review(state)

                if verdict == "PASS":
                    break

                if verdict is None:
                    return self._stop(state, "review_no_verdict")

                if state["revisions"] >= self.max_revisions:
                    return self._stop(state, "max_revisions")

                state["revisions"] += 1

                if not self._plan(state, revise=True):
                    return self._stop(state, "revise_failed")

            if not self._code(state):
                error = state["steps"].get("coding", {}).get("error") or ""
                reason = (
                    error if error in CODE_FAILURE_REASONS else "code_failed"
                )

                return self._stop(state, reason)

            if self._verify(state) != "PASS":
                return self._stop(state, "acceptance_failed")

            state["state"] = "done"
            state["step"] = "done"
            self._history(state, "done", artifacts=len(state.get("artifacts") or []))
            self.save(state)
            self._notify(
                f"[编排 {run_id}] 完成：产物 {len(state.get('artifacts') or [])} 个，"
                f"目录 {self.run_dir(run_id)}"
            )

            return state
        except Exception as error:  # noqa: BLE001 - 任何异常都不许留 running
            self._step_failed(state, state.get("step") or "unknown", f"exception: {error}")

            return self._stop(state, f"exception: {error}")

    def _stop(self, state: dict, reason: str) -> dict:
        state["state"] = "stopped"
        state["stop_reason"] = reason
        self._history(state, "stopped", reason=reason)
        self.save(state)
        artifacts = len(state.get("artifacts") or [])
        self._notify(
            f"[编排 {state['run_id']}] 停止：{reason}；已生成产物 {artifacts} 个"
            f"（{self.run_dir(state['run_id'])}）"
        )

        return state

    def status(self, run_id: str | None = None) -> dict | None:
        if run_id is None:
            candidates = sorted(
                (path for path in self.root.glob("run-*") if path.is_dir()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )

            if not candidates:
                return None

            run_id = candidates[0].name

        try:
            state = self.load(run_id)
        except (OrchestratorError, OSError, json.JSONDecodeError):
            return None

        steps = state.get("steps") or {}
        failed_step = next(
            (
                name for name, step in steps.items()
                if isinstance(step, dict) and step.get("ok") is False
            ),
            None,
        )
        artifacts = len(state.get("artifacts") or [])
        stop_reason = state.get("stop_reason") or ""

        if state["state"] == "stopped":
            message = f"已停止：{stop_reason or 'unknown'}"

            if failed_step:
                message += f"（失败步骤 {failed_step}）"
        elif state["state"] == "done":
            message = f"已完成，产物 {artifacts} 个"
        else:
            message = f"{state['state']}/{state.get('step') or '-'}"

        message += f"（返工 {state.get('revisions')}/{state.get('max_revisions')}）"

        return {
            "run_id": state["run_id"],
            "state": state["state"],
            "step": state.get("step"),
            "revisions": state.get("revisions"),
            "stop_reason": stop_reason,
            "failed_step": failed_step,
            "artifacts": artifacts,
            "message": message,
        }


def uuid4_short() -> str:
    import uuid

    return uuid.uuid4().hex[:6]
