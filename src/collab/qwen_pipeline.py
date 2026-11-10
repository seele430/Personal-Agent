"""Qwen Runner 结果接入：独立 Verify → Registry → 通知。

原则（沿用 verify.py）：
- 不采信 Runner 的 manifest 哈希，自己重算；
- 外部 claims 只存档，不参与判定；
- 通知仅在 Verify 通过后发送，失败不影响验收。
"""

from __future__ import annotations

import hashlib
import json

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from src.collab.qwen_runner import status_of
from src.collab.qwen_workspace import RunWorkspace
from src.collab.registry import ExternalTaskRegistry, TaskNotFound


PROVIDER = "qwen-cli"
READY_STATE = "completed_unverified"


class PipelineError(ValueError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class VerifyReport:
    passed: bool
    checks: tuple[Check, ...] = field(default_factory=tuple)
    artifacts: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "artifacts": list(self.artifacts),
            "checks": [
                {"name": check.name, "ok": check.ok, "detail": check.detail}
                for check in self.checks
            ],
        }


def verify_run(root: str | Path, run_id: str, expected: tuple[str, ...] = ()) -> VerifyReport:
    state = status_of(root, run_id)

    if state is None:
        raise PipelineError("not_found", f"run 不存在：{run_id}")

    if state.get("state") != READY_STATE:
        raise PipelineError("not_ready", f"状态 {state.get('state')} 不能验收")

    workspace = RunWorkspace(root, run_id)
    manifest_path = workspace.manifest_path

    if not manifest_path.exists():
        raise PipelineError("missing_manifest", "manifest 不存在")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise PipelineError("invalid_manifest", "manifest 不可读") from None

    root_path = workspace.workspace.resolve()
    checks: list[Check] = []
    artifacts: list[str] = []

    for item in manifest.get("artifacts", []):
        relative = item.get("path", "")
        target = (workspace.workspace / relative).resolve()

        if not relative or not target.is_relative_to(root_path):
            checks.append(Check(f"path:{relative}", False, "路径越界"))
            continue

        if not target.is_file():
            checks.append(Check(f"exists:{relative}", False, "文件不存在"))
            continue

        data = target.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        checks.append(Check(f"sha256:{relative}", digest == item.get("sha256"), "哈希校验"))
        checks.append(Check(f"bytes:{relative}", len(data) == item.get("bytes"), "字节数校验"))
        artifacts.append(relative)

    for relative in expected:
        target = workspace.workspace / relative
        checks.append(
            Check(
                f"expected:{relative}",
                target.is_file() and target.stat().st_size > 0,
                "预期产物存在且非空",
            )
        )

    if not checks:
        checks.append(Check("artifacts", False, "没有可验收的产物"))

    return VerifyReport(
        passed=all(check.ok for check in checks),
        checks=tuple(checks),
        artifacts=tuple(artifacts),
    )


def finalize_run(
    *,
    root: str | Path,
    run_id: str,
    registry: ExternalTaskRegistry,
    expected: tuple[str, ...] = (),
    sender=None,
) -> dict:
    report = verify_run(root, run_id, expected)
    state = status_of(root, run_id) or {}
    try:
        existing = registry.get(PROVIDER, run_id)
    except TaskNotFound:
        existing = None

    claims = {
        "observed_model": state.get("observed_model"),
        "duration_ms": state.get("duration_ms"),
        "usage": state.get("usage"),
        "approval_mode": state.get("approval_mode"),
    }

    if existing and existing.claims.get("notified_at"):
        claims["notified_at"] = existing.claims["notified_at"]

    registry.register(
        PROVIDER,
        run_id,
        run_id=run_id,
        state="submitted",
        artifacts=report.artifacts,
        claims=claims,
    )
    target_state = "succeeded" if report.passed else "failed"
    registry.update_state(PROVIDER, run_id, target_state)
    notified = False
    notification_error = None
    task = registry.get(PROVIDER, run_id)

    if task.claims.get("notified_at"):
        claims["notified_at"] = task.claims["notified_at"]

    if report.passed and sender is not None and not task.claims.get("notified_at"):
        text = (
            f"Qwen 任务完成：{run_id[:8]}，"
            f"{len(report.artifacts)} 个产物，"
            f"耗时 {(state.get('duration_ms') or 0) / 1000:.1f}s"
        )

        try:
            sender(text)
            notified = True
            claims["notified_at"] = datetime.now(timezone.utc).isoformat()
        except Exception as error:  # noqa: BLE001 - 通知失败不应回滚验收
            notification_error = str(error)[:200]
    elif task.claims.get("notified_at"):
        notified = True

    registry.register(
        PROVIDER,
        run_id,
        run_id=run_id,
        state=target_state,
        artifacts=report.artifacts,
        claims=claims,
    )

    return {
        "run_id": run_id,
        "state": target_state,
        "verify": report.to_dict(),
        "notified": notified,
        "notification_error": notification_error,
    }
