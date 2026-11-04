"""工具审批：状态、参数摘要与等待（D13）。

约束（见 ``docs/d13-approval-plan.md``）：

- **审批前不产生 exec**：批准之后才创建 exec，仍然一次调用一条 exec。
- **批准的是「这一次调用」**：参数在批准时冻结，执行前用摘要校验；参数变了
  必须重新审批。
- **拒绝 / 超时 / run 取消都是终态**，都不产生 exec。
- **审批是产品控制，不是隔离**：批准 shell 不等于让 shell 变安全。

真相在 SQLite（``agent_approvals``），本模块只提供摘要有序化、预览生成和
等待逻辑；等待用轮询，决策可以来自任意线程 / 任意进程。
"""

from __future__ import annotations

import hashlib
import json
import os
import time

from typing import Any

from src.core.tool_registry import ToolDenied

APPROVAL_TIMEOUT_ENV = "PERSONAL_AGENT_APPROVAL_TIMEOUT_SECONDS"
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 300

PREVIEW_LIMIT = 600
POLL_INTERVAL_SECONDS = 0.05

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXPIRED = "expired"

DECISION_TO_STATUS = {"approve": APPROVED, "reject": REJECTED}


def approval_timeout_seconds() -> float:
    """审批等待上限；测试用环境变量缩短，默认 5 分钟。"""

    raw = os.getenv(APPROVAL_TIMEOUT_ENV, "").strip()

    if not raw:
        return float(DEFAULT_APPROVAL_TIMEOUT_SECONDS)

    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_APPROVAL_TIMEOUT_SECONDS)

    return value if value > 0 else float(DEFAULT_APPROVAL_TIMEOUT_SECONDS)


def canonical_arguments(arguments: dict[str, Any]) -> str:
    return json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def digest_arguments(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(
        canonical_arguments(arguments).encode("utf-8")
    ).hexdigest()


def build_preview(tool_name: str, arguments: dict[str, Any]) -> str:
    """给用户看的参数摘要；只做截断，不在这里做脱敏以外的加工。"""

    text = f"{tool_name} {canonical_arguments(arguments)}"

    if len(text) <= PREVIEW_LIMIT:
        return text

    return text[:PREVIEW_LIMIT] + "…"


def verify_digest(approval: dict[str, Any], arguments: dict[str, Any]) -> None:
    """执行前校验参数没有被换过。"""

    if approval.get("arguments_digest") != digest_arguments(arguments):
        raise ToolDenied("审批参数与执行参数不一致，已拒绝执行")


def wait_for_decision(
    store,
    approval_id: str,
    *,
    cancel_token=None,
    timeout: float | None = None,
) -> str:
    """等待审批结果。

    返回 ``approved`` / ``rejected`` / ``expired`` 之一，或 ``cancelled``
    表示 run 在等待期间被取消（此时调用方负责把审批置为 expired）。
    """

    deadline = time.monotonic() + (
        approval_timeout_seconds() if timeout is None else timeout
    )

    while True:
        if cancel_token is not None and cancel_token.cancelled:
            return "cancelled"

        approval = store.get_approval(approval_id)

        if approval["status"] != PENDING:
            return approval["status"]

        if time.monotonic() >= deadline:
            store.expire_approval(approval_id)
            return EXPIRED

        time.sleep(POLL_INTERVAL_SECONDS)


def decide_for_request(
    store,
    *,
    request_id: str,
    approval_id: str,
    user_id: str,
    decision: str,
) -> dict[str, Any]:
    """HTTP 层用的决定入口：归属校验 + 状态迁移。

    - run 不存在或不属于该用户 → ``KeyError``（HTTP 404，不泄露存在性）
    - 审批已决定过 → ``ValueError``（HTTP 409）
    """

    run = store.find_run_by_request(request_id, owner_id=user_id)

    if run is None:
        raise KeyError(request_id)

    approval = store.get_approval(approval_id)

    if approval["run_id"] != run["id"]:
        raise KeyError(approval_id)

    return store.decide_approval(
        approval_id, decision=decision, decided_by=user_id
    )
