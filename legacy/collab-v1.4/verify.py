"""独立验收：只认**本地执行结果**，不采信外部 Agent 自述。

路线图 §6.1 非目标第 6 条："不把外部 Agent 自己报告的'测试通过'当作
Personal Agent 的验收证据"。所以：

- `claims` 只存档，`verify()` 不读它；
- 判定只看本地检查（产物存在、文件哈希、可选命令退出码）。
"""

from __future__ import annotations

import hashlib
import subprocess

from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_TIMEOUT = 120


class VerifyError(ValueError):
    """参数不合法。"""


@dataclass(frozen=True)
class VerifyCheck:
    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class VerifyResult:
    passed: bool
    checks: tuple[VerifyCheck, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "checks": [
                {"name": check.name, "ok": check.ok, "detail": check.detail}
                for check in self.checks
            ],
        }


def check_artifacts(artifact_dir: str | Path, expected: tuple[str, ...]) -> VerifyCheck:
    root = Path(artifact_dir)
    missing = [name for name in expected if not (root / name).is_file()]

    if missing:
        return VerifyCheck("artifacts", False, f"缺少：{missing}")

    return VerifyCheck("artifacts", True, f"{len(expected)} 个产物存在")


def check_digest(path: str | Path, expected_sha256: str) -> VerifyCheck:
    target = Path(path)

    if not target.is_file():
        return VerifyCheck("digest", False, f"文件不存在：{target}")

    digest = hashlib.sha256(target.read_bytes()).hexdigest()

    if digest != expected_sha256:
        return VerifyCheck("digest", False, "哈希不匹配")

    return VerifyCheck("digest", True, "哈希一致")


def run_command(
    argv: list[str],
    *,
    cwd: str | Path,
    timeout: int = DEFAULT_TIMEOUT,
) -> VerifyCheck:
    """在本地跑一条只读检查命令，只认退出码。"""

    if not argv:
        raise VerifyError("argv 不能为空")

    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )

    except subprocess.TimeoutExpired:
        return VerifyCheck("command", False, f"超时（{timeout}s）")

    except OSError as error:
        return VerifyCheck("command", False, f"无法执行：{error}")

    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip()[-200:]

        return VerifyCheck("command", False, f"exit={completed.returncode} {tail}")

    return VerifyCheck("command", True, "exit=0")


def verify(
    *,
    artifact_dir: str | Path,
    expected: tuple[str, ...] = (),
    command: list[str] | None = None,
    cwd: str | Path | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> VerifyResult:
    checks = []

    if expected:
        checks.append(check_artifacts(artifact_dir, expected))

    if command:
        checks.append(run_command(command, cwd=cwd or artifact_dir, timeout=timeout))

    if not checks:
        raise VerifyError("至少要有一种检查（expected 或 command）")

    return VerifyResult(passed=all(check.ok for check in checks), checks=tuple(checks))
