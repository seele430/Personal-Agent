"""Qwen 运行目录监听：扫描 completed_unverified → 自动 finalize。

边界：
- 只轮询指定根目录，不监听文件系统事件；
- 只处理 UUID 目录与 `completed_unverified`；
- 单个坏 run 不影响其它 run；不删除、不移动任何目录。
"""

from __future__ import annotations

import json
import re

from pathlib import Path

from src.collab.qwen_pipeline import PipelineError, finalize_run
from src.collab.registry import ExternalTaskRegistry, TaskNotFound


UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
READY_STATE = "completed_unverified"


def scan_once(
    *,
    root: str | Path,
    registry: ExternalTaskRegistry,
    sender=None,
) -> dict:
    root_path = Path(root)
    processed: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []

    if not root_path.exists():
        return {"processed": processed, "skipped": skipped, "errors": errors}

    for entry in sorted(root_path.iterdir()):
        if not entry.is_dir() or not UUID_PATTERN.match(entry.name):
            continue

        state_path = entry / "state.json"

        if not state_path.exists():
            continue

        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            errors.append({"run_id": entry.name, "error": "invalid_state", "detail": str(error)[:200]})
            continue

        if state.get("state") != READY_STATE:
            skipped.append({"run_id": entry.name, "reason": f"state={state.get('state')}"})
            continue

        try:
            existing = registry.get("qwen-cli", entry.name)
        except TaskNotFound:
            existing = None

        if existing is not None and existing.state in ("succeeded", "failed"):
            skipped.append({"run_id": entry.name, "reason": f"registry={existing.state}"})
            continue

        try:
            report = finalize_run(
                root=root_path,
                run_id=entry.name,
                registry=registry,
                sender=sender,
            )
            processed.append({
                "run_id": entry.name,
                "state": report["state"],
                "verify_passed": report["verify"]["passed"],
                "notified": report["notified"],
            })
        except PipelineError as error:
            errors.append({"run_id": entry.name, "error": error.code, "detail": str(error)[:200]})
        except Exception as error:  # noqa: BLE001 - 单条失败不阻塞扫描
            errors.append({"run_id": entry.name, "error": "unexpected", "detail": str(error)[:200]})

    return {"processed": processed, "skipped": skipped, "errors": errors}
