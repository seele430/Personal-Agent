"""代码修复闭环：跑测试 → 读失败 → 修复 → 重跑 → 评分。

四条硬约束：

1. **有界**：最多 `max_rounds` 轮（默认 3），单轮超时 60 s，单文件 16 KB；
2. **不碰仓库**：所有写操作只发生在**临时目录里的夹具副本**上；
3. **执行器与修复器都可注入**：离线测试用脚本化替身，真实运行走
   `src.research.llm`（W2 建的模型层）；
4. **失败不包装**：三轮不过就记 `failed`，保留每一轮的输出摘要。

诚实边界：默认执行器是**受限子进程**（独立临时目录 + 超时 + 输出截断），
不是平台 tier1 沙箱。沙箱是同一个 `runner` 接口的另一种实现，本版没有接。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import time

from dataclasses import dataclass, field
from pathlib import Path

from src.repair.fixtures import BugFixture


MAX_ROUNDS = 3
MAX_FILE_BYTES = 16 * 1024
ROUND_TIMEOUT_SECONDS = 60
MAX_OUTPUT_CHARS = 4000

FAILURE_LINE = re.compile(
    r"^(?:FAILED \((?P<detail>[^)]*)\)|(?P<ok>OK))", re.MULTILINE
)
COUNT = re.compile(r"(failures|errors)=(\d+)")


class RepairError(RuntimeError):
    """修复流程本身出错（配置/环境），不是"没修好"。"""


@dataclass(frozen=True)
class RunResult:
    ok: bool
    failures: int
    errors: int
    output: str
    duration_ms: float

    @property
    def problem_count(self) -> int:
        return self.failures + self.errors


@dataclass(frozen=True)
class RepairAttempt:
    round_index: int
    ok: bool
    failures: int
    errors: int
    output_excerpt: str
    changed_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepairResult:
    fixture: str
    success: bool
    rounds: int
    score: float
    attempts: tuple[RepairAttempt, ...] = ()
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "fixture": self.fixture,
            "success": self.success,
            "rounds": self.rounds,
            "score": round(self.score, 4),
            "error": self.error,
            "attempts": [
                {
                    "round": item.round_index,
                    "ok": item.ok,
                    "failures": item.failures,
                    "errors": item.errors,
                    "changed_files": list(item.changed_files),
                    "output": item.output_excerpt,
                }
                for item in self.attempts
            ],
        }


def parse_failures(output: str) -> tuple[int, int]:
    """从 unittest 输出里数出 failures / errors。"""

    failures = 0
    errors = 0

    for match in COUNT.finditer(output or ""):
        kind, value = match.group(1), int(match.group(2))

        if kind == "failures":
            failures += value
        else:
            errors += value

    return failures, errors


def default_runner(
    files: dict[str, str],
    *,
    modules: tuple[str, ...],
    timeout: float = ROUND_TIMEOUT_SECONDS,
) -> RunResult:
    """在临时目录里跑 unittest —— 不碰仓库、超时即杀。"""

    started = time.perf_counter()
    workdir = Path(tempfile.mkdtemp(prefix="pa-repair-"))

    try:
        for name, content in files.items():
            target = workdir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        argv = [sys.executable, "-m", "unittest", *modules, "-v"]

        try:
            completed = subprocess.run(
                argv,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

        except subprocess.TimeoutExpired:
            return RunResult(
                ok=False,
                failures=0,
                errors=1,
                output=f"执行超时（{timeout:.0f}s）",
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )

        output = (completed.stderr or "") + (completed.stdout or "")
        failures, errors = parse_failures(output)
        ok = completed.returncode == 0 and failures == 0 and errors == 0

        return RunResult(
            ok=ok,
            failures=failures,
            errors=errors,
            output=output[:MAX_OUTPUT_CHARS],
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )

    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def default_fixer(fixture: BugFixture, failure: str, files: dict[str, str]) -> dict:
    """真实修复：让模型返回完整文件内容（不是 diff，避免补丁应用歧义）。"""

    from src.research.llm import complete_json

    listing = "\n\n".join(
        f"### 文件 {name}\n```python\n{content}```" for name, content in files.items()
    )

    messages = [
        {
            "role": "system",
            "content": (
                "你是修 bug 的工程师。只输出 JSON，不要解释。\n"
                "输出格式：{\"files\": {\"文件名\": \"完整的新内容\"}}。\n"
                "只允许修改给定文件；不确定时保持原样。\n"
            ),
        },
        {
            "role": "user",
            "content": (
                f"缺陷描述：{fixture.description}\n\n"
                f"当前文件：\n{listing}\n\n"
                f"测试失败输出：\n{failure[:2000]}\n\n"
                "请给出修复后的完整文件内容。"
            ),
        },
    ]

    payload, _call = complete_json(messages, max_tokens=1536, temperature=0.0)
    changed = payload.get("files")

    if not isinstance(changed, dict):
        raise RepairError("模型没有返回 files 对象")

    return changed


class RepairLoop:
    def __init__(
        self,
        fixture: BugFixture,
        *,
        runner=None,
        fixer=None,
        max_rounds: int = MAX_ROUNDS,
        timeout: float = ROUND_TIMEOUT_SECONDS,
    ):
        if isinstance(max_rounds, bool) or not isinstance(max_rounds, int):
            raise ValueError("max_rounds 必须是整数")

        if not 1 <= max_rounds <= 5:
            raise ValueError("max_rounds 必须在 1..5")

        self.fixture = fixture
        self.runner = runner or default_runner
        self.fixer = fixer or default_fixer
        self.max_rounds = max_rounds
        self.timeout = timeout

    def run(self) -> RepairResult:
        files = dict(self.fixture.files)
        attempts: list[RepairAttempt] = []

        first = self._run(files)

        attempts.append(
            RepairAttempt(
                round_index=0,
                ok=first.ok,
                failures=first.failures,
                errors=first.errors,
                output_excerpt=first.output[:600],
            )
        )

        if first.ok:
            return RepairResult(
                fixture=self.fixture.name,
                success=True,
                rounds=0,
                score=1.0,
                attempts=tuple(attempts),
            )

        last = first

        for round_index in range(1, self.max_rounds + 1):
            try:
                proposed = self.fixer(self.fixture, last.output, dict(files))

            except RepairError as error:
                return RepairResult(
                    fixture=self.fixture.name,
                    success=False,
                    rounds=round_index - 1,
                    score=0.0,
                    attempts=tuple(attempts),
                    error=str(error),
                )

            try:
                changed = self._apply(files, proposed)

            except RepairError as error:
                return RepairResult(
                    fixture=self.fixture.name,
                    success=False,
                    rounds=round_index - 1,
                    score=0.0,
                    attempts=tuple(attempts),
                    error=str(error),
                )

            last = self._run(files)

            attempts.append(
                RepairAttempt(
                    round_index=round_index,
                    ok=last.ok,
                    failures=last.failures,
                    errors=last.errors,
                    output_excerpt=last.output[:600],
                    changed_files=tuple(changed),
                )
            )

            if last.ok:
                return RepairResult(
                    fixture=self.fixture.name,
                    success=True,
                    rounds=round_index,
                    score=1.0,
                    attempts=tuple(attempts),
                )

        solved = max(0, self.fixture.test_count - last.problem_count)
        score = solved / self.fixture.test_count if self.fixture.test_count else 0.0

        return RepairResult(
            fixture=self.fixture.name,
            success=False,
            rounds=self.max_rounds,
            score=score,
            attempts=tuple(attempts),
            error=f"{self.max_rounds} 轮后仍未通过",
        )

    def _run(self, files: dict[str, str]) -> RunResult:
        return self.runner(files, modules=self.fixture.test_modules, timeout=self.timeout)

    def _apply(self, files: dict[str, str], proposed) -> list[str]:
        """只接受夹具里已存在的文件，且有大小上限。"""

        if not isinstance(proposed, dict):
            raise RepairError("修复结果必须是 {文件名: 内容}")

        if not proposed:
            raise RepairError("修复结果没有改动任何文件")

        allowed = set(self.fixture.all_paths())
        changed = []

        for name, content in proposed.items():
            if name not in allowed:
                raise RepairError(f"修复试图写夹具之外的文件：{name}")

            if not isinstance(content, str):
                raise RepairError(f"{name} 的内容不是字符串")

            if len(content.encode("utf-8")) > MAX_FILE_BYTES:
                raise RepairError(f"{name} 超过 {MAX_FILE_BYTES} 字节上限")

            if files.get(name) == content:
                # 内容没变就不算改动：既不报错，也不假装修过。
                continue

            files[name] = content
            changed.append(name)

        return sorted(changed)


def run_fixture(name: str, *, fixer=None, runner=None, max_rounds: int = MAX_ROUNDS):
    from src.repair.fixtures import get_fixture

    return RepairLoop(
        get_fixture(name),
        fixer=fixer,
        runner=runner,
        max_rounds=max_rounds,
    ).run()
