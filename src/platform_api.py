"""平台 v1 HTTP API（D15 续）：沙箱 create / get / destroy 与 exec。

接口形状与稳定错误码以 ``docs/platform-contract.md`` 和
``sdk/fixtures/v1/contracts.json`` 为准；SDK 用 ``UrllibTransport`` 直接打这里。

尚未实现（不在本次范围）：``/v1/sandboxes/{id}/fn``、``/v1/nodes``、
``/metrics``、TTL / reaper / 410 之外的 tombstone 语义（D16—D20、D25）。
"""

from __future__ import annotations

import threading

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.core.run_control import CancellationToken
from src.platform import active_execs
from src.platform.exec_service import (
    ERROR_OUTPUT_LIMIT,
    EXEC_CANCELLED,
    EXEC_FAILED,
    EXEC_SUCCEEDED,
    EXEC_TIMED_OUT,
    Tier1Executor,
)
from src.platform.exec_store import (
    EXEC_CANCELLED as STORE_CANCELLED,
    TERMINAL_EXEC_STATES,
    ExecStore,
)
from src.platform.sandbox_service import (
    PlatformError,
    SandboxNotFound,
    SandboxService,
)

router = APIRouter(prefix="/v1", tags=["沙箱平台"])

NODE_ID = "node_local"


# ==========================================
# 请求模型
# ==========================================

class CreateSandboxRequest(BaseModel):
    tier: str = "tier1"
    workspace_id: str | None = None
    ttl_seconds: int = 600


class ExecRequest(BaseModel):
    command: list[str]
    run_id: str | None = None
    step_id: str | None = None
    timeout_ms: int = 30_000
    max_output_bytes: int = 65_536


# ==========================================
# 工具函数
# ==========================================

def _error(
    status: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
                "request_id": None,
            }
        },
    )


def _sandbox_body(sandbox: dict[str, Any]) -> dict[str, Any]:
    return {
        "sandbox_id": sandbox["id"],
        "state": sandbox["state"],
        "node_id": NODE_ID,
        "workspace_id": sandbox["workspace_id"],
        "created_at": sandbox["created_at"],
        "expires_at": sandbox["expires_at"],
        "ttl_seconds": sandbox["ttl_seconds"],
        "reclaimed_at": sandbox.get("reclaimed_at"),
        "reclaim_reason": sandbox.get("reclaim_reason"),
    }


def _exec_body(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "exec_id": row["id"],
        "sandbox_id": row["sandbox_id"],
        "state": row["state"],
        "timeout_ms": row["timeout_ms"],
        "max_output_bytes": row["max_output_bytes"],
        "exit_code": row["exit_code"],
        "stdout": row["stdout"] or "",
        "stderr": row["stderr"] or "",
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "truncated": bool(row["truncated"]),
        "result_code": row["result_code"],
    }


def _result_code(outcome) -> str:
    if outcome.status == EXEC_SUCCEEDED:
        return "EXEC_SUCCEEDED"

    if outcome.status == EXEC_TIMED_OUT:
        return "EXEC_TIMED_OUT"

    if outcome.status == EXEC_CANCELLED:
        return "EXEC_CANCELLED"

    if outcome.error_code == ERROR_OUTPUT_LIMIT:
        return "OUTPUT_LIMIT_EXCEEDED"

    return "EXEC_FAILED"


def _sandbox_expired(sandbox: dict[str, Any]) -> bool:
    """沙箱是否已经越过 TTL。

    契约要求「到期后 reaper 先取消活动 exec、清进程树，再置 reclaimed」；
    这里做惰性兜底：reaper 还没跑到时，访问本身也会触发同一条回收路径。
    """

    expires_at = sandbox.get("expires_at")

    if not expires_at:
        return False

    try:
        deadline = datetime.fromisoformat(expires_at)
    except ValueError:
        return False

    return datetime.now(timezone.utc) >= deadline


def _require_sandbox(sandbox_id: str, *, allow_reclaimed: bool = False):
    """返回 (sandbox, None) 或 (None, error_response)。"""

    service = SandboxService()

    try:
        sandbox = service.get(sandbox_id)
    except SandboxNotFound:
        return None, _error(404, "SANDBOX_NOT_FOUND", "沙箱不存在")

    if sandbox["state"] == "created" and _sandbox_expired(sandbox):
        active_execs.cancel_for_sandbox(sandbox_id)
        sandbox = service.reclaim(sandbox_id, reason="ttl_expired")

    if sandbox["state"] == "reclaimed" and not allow_reclaimed:
        return None, _error(410, "SANDBOX_GONE", "沙箱已回收")

    return sandbox, None


def _run_exec(
    exec_id: str,
    sandbox_id: str,
    argv: list[str],
    timeout_ms: int,
    max_output_bytes: int,
) -> None:
    store = ExecStore()
    token = CancellationToken()

    active_execs.register(exec_id, sandbox_id, token)

    try:
        outcome = Tier1Executor().execute_command(
            argv,
            sandbox_id=sandbox_id,
            cancel_token=token,
            timeout_ms=timeout_ms,
            max_output_bytes=max_output_bytes,
            on_started=lambda _sandbox: store.start(exec_id),
        )

        store.finish(
            exec_id,
            state=outcome.status,
            exit_code=outcome.exit_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            truncated=outcome.truncated,
            error_code=outcome.error_code,
            result_code=_result_code(outcome),
        )
    except Exception as exc:  # noqa: BLE001 - 执行器崩溃也要给确定终态
        store.finish(
            exec_id,
            state=EXEC_FAILED,
            exit_code=None,
            stdout="",
            stderr=str(exc),
            truncated=False,
            error_code="INTERNAL_ERROR",
            result_code="EXEC_FAILED",
        )
    finally:
        active_execs.unregister(exec_id)


# ==========================================
# 沙箱
# ==========================================

@router.post("/sandboxes", status_code=201)
def create_sandbox(
    payload: CreateSandboxRequest,
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key"
    ),
):
    try:
        sandbox = SandboxService().create(
            idempotency_key=idempotency_key or "",
            tier=payload.tier,
            workspace_id=payload.workspace_id,
            ttl_seconds=payload.ttl_seconds,
        )
    except PlatformError as exc:
        return _error(exc.http_status, exc.code, str(exc))

    return JSONResponse(status_code=201, content=_sandbox_body(sandbox))


@router.get("/sandboxes/{sandbox_id}")
def get_sandbox(sandbox_id: str):
    sandbox, error = _require_sandbox(sandbox_id)

    if error is not None:
        return error

    return _sandbox_body(sandbox)


@router.post("/sandboxes/{sandbox_id}/destroy")
def destroy_sandbox(sandbox_id: str):
    sandbox, error = _require_sandbox(sandbox_id, allow_reclaimed=True)

    if error is not None:
        return error

    return _sandbox_body(SandboxService().reclaim(sandbox_id, reason="manual"))


# ==========================================
# exec
# ==========================================

@router.post("/sandboxes/{sandbox_id}/exec", status_code=202)
def create_exec(sandbox_id: str, payload: ExecRequest):
    sandbox, error = _require_sandbox(sandbox_id)

    if error is not None:
        return error

    command = payload.command

    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) and item for item in command)
    ):
        return _error(400, "INVALID_ARGUMENT", "command 必须是非空字符串数组")

    if not 1 <= payload.timeout_ms <= 300_000:
        return _error(400, "INVALID_ARGUMENT", "timeout_ms 必须在 1—300000")

    if not 1 <= payload.max_output_bytes <= 1_048_576:
        return _error(
            400, "INVALID_ARGUMENT", "max_output_bytes 必须在 1—1048576"
        )

    row = ExecStore().create(
        sandbox_id,
        argv=command,
        run_id=payload.run_id,
        step_id=payload.step_id,
        timeout_ms=payload.timeout_ms,
        max_output_bytes=payload.max_output_bytes,
    )

    threading.Thread(
        target=_run_exec,
        args=(
            row["id"],
            sandbox_id,
            command,
            payload.timeout_ms,
            payload.max_output_bytes,
        ),
        daemon=True,
    ).start()

    return JSONResponse(status_code=202, content=_exec_body(row))


@router.get("/exec/{exec_id}")
def get_exec(exec_id: str):
    try:
        row = ExecStore().get(exec_id)
    except KeyError:
        return _error(404, "EXEC_NOT_FOUND", "exec 不存在")

    return _exec_body(row)


@router.post("/exec/{exec_id}/cancel", status_code=202)
def cancel_exec(exec_id: str):
    try:
        row = ExecStore().get(exec_id)
    except KeyError:
        return _error(404, "EXEC_NOT_FOUND", "exec 不存在")

    if row["state"] == STORE_CANCELLED:
        # 已经取消：幂等返回当前结果。
        return JSONResponse(status_code=202, content=_exec_body(row))

    if row["state"] in TERMINAL_EXEC_STATES:
        return _error(409, "EXEC_NOT_CANCELLABLE", "exec 已到不可取消的终态")

    active_execs.cancel(exec_id)

    # 返回完整资源：SDK 的 cancel() 会按 ExecResult 解析，缺字段会炸。
    return JSONResponse(status_code=202, content=_exec_body(row))
