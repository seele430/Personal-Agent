"""平台 v1 HTTP API（D15 续）：沙箱 create / get / destroy 与 exec。

接口形状与稳定错误码以 ``docs/platform-contract.md`` 和
``sdk/fixtures/v1/contracts.json`` 为准；SDK 用 ``UrllibTransport`` 直接打这里。

尚未实现（不在本次范围）：``/v1/sandboxes/{id}/fn``、``/metrics``。
D25 起 ``/v1`` 可用 Bearer 令牌保护（中间件在 ``src/main.py``）；
D26 增加限流与 ``GET /v1/audit``。
"""

from __future__ import annotations

import hashlib
import logging
import os

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.platform import ops as platform_ops
from src.platform.audit import EVENTS, AuditLog
from src.platform.node_registry import (
    InvalidNodeState,
    NodeBusy,
    NodeNotFound,
    NodeRegistry,
    StaleGeneration,
)
from src.platform.exec_store import ExecStore
from src.platform.sandbox_service import PlatformError, SandboxService

router = APIRouter(prefix="/v1", tags=["沙箱平台"])

logger = logging.getLogger("personal_agent.platform.api")


def _default_node_id() -> str:
    """单机部署下所有沙箱都在默认节点上；顺手保证节点表里有这一行。"""

    return NodeRegistry().ensure_default_node()["node_id"]


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


class RegisterNodeRequest(BaseModel):
    node_id: str | None = None
    capacity: int = 4
    heartbeat_interval_seconds: int = 10


class HeartbeatRequest(BaseModel):
    generation: int


# ==========================================
# 工具函数
# ==========================================

def _error(
    status: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    headers: dict[str, str] | None = None,
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
        headers=headers,
    )


def _queue_retry_after() -> int:
    """队列满时给调用方的退避提示（秒）。

    没有真实的 exec 时长统计可用，所以这是一个**可配置的估计值**，
    不是精确等待时间；文档里写清楚。
    """

    raw = os.getenv("PERSONAL_AGENT_PLATFORM_QUEUE_RETRY_AFTER", "").strip()

    try:
        value = int(raw)
    except ValueError:
        value = 5

    return value if value >= 1 else 5


def _queue_full_error() -> JSONResponse:
    return _error(
        429,
        "QUEUE_FULL",
        "执行队列已满，请稍后重试",
        retryable=True,
        headers={"Retry-After": str(_queue_retry_after())},
    )


def _sandbox_body(sandbox: dict[str, Any]) -> dict[str, Any]:
    return {
        "sandbox_id": sandbox["id"],
        "state": sandbox["state"],
        "node_id": sandbox.get("node_id") or _default_node_id(),
        "workspace_id": sandbox["workspace_id"],
        "created_at": sandbox["created_at"],
        "expires_at": sandbox["expires_at"],
        "ttl_seconds": sandbox["ttl_seconds"],
        "reclaimed_at": sandbox.get("reclaimed_at"),
        "reclaim_reason": sandbox.get("reclaim_reason"),
        "tenant_id": sandbox.get("tenant_id"),
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
        "traceparent": row.get("traceparent"),
    }


def _require_sandbox(sandbox_id: str, *, allow_reclaimed: bool = False):
    """返回 (sandbox, None) 或 (None, error_response)。

    业务校验在 ``src.platform.ops``（与 /console 共用），这里只做 HTTP 映射。
    """

    try:
        sandbox = platform_ops.require_sandbox(
            sandbox_id, allow_reclaimed=allow_reclaimed
        )
    except PlatformError as exc:
        return None, _error(
            exc.http_status,
            exc.code,
            str(exc),
            retryable=bool(getattr(exc, "retryable", False)),
        )

    return sandbox, None


def _exec_queue():
    """兼容旧调用点：队列与 worker 已搬到 ``src.platform.ops``。"""

    return platform_ops.exec_queue()


# ==========================================
# 节点（D17）
# ==========================================

def _node_body(
    row: dict[str, Any], *, active_sandboxes: int | None = None
) -> dict[str, Any]:
    return {
        "node_id": row["node_id"],
        "generation": row["generation"],
        "state": row["state"],
        "capacity": row["capacity"],
        "registered_at": row["registered_at"],
        "last_heartbeat": row["last_heartbeat"],
        "heartbeat_interval_seconds": row.get(
            "heartbeat_interval_seconds", 10
        ),
        "lease_seconds": row.get("lease_seconds"),
        "lease_expires_at": row.get("lease_expires_at"),
        "seconds_since_heartbeat": row.get("seconds_since_heartbeat"),
        "expired": row.get("expired", False),
        "cordoned": row.get("cordoned", False),
        "draining": row.get("draining", False),
        "removed": row.get("removed", False),
        "accepting": row.get("accepting", False),
        "active_sandboxes": active_sandboxes,
    }


def _active_sandboxes(node_id: str) -> int:
    return SandboxService().count_active(node_id)


@router.post("/nodes", status_code=201)
def register_node(payload: RegisterNodeRequest):
    try:
        row = NodeRegistry().register(
            node_id=payload.node_id,
            capacity=payload.capacity,
            heartbeat_interval_seconds=payload.heartbeat_interval_seconds,
        )
    except ValueError as exc:
        return _error(400, "INVALID_ARGUMENT", str(exc))

    return JSONResponse(status_code=201, content=_node_body(row))


@router.get("/nodes")
def list_nodes():
    rows = NodeRegistry().list_nodes()
    return {
        "nodes": [
            _node_body(row, active_sandboxes=_active_sandboxes(row["node_id"]))
            for row in rows
        ]
    }


@router.post("/nodes/{node_id}/heartbeat", status_code=202)
def heartbeat(node_id: str, payload: HeartbeatRequest):
    try:
        row = NodeRegistry().heartbeat(
            node_id, generation=payload.generation
        )
    except NodeNotFound:
        return _error(404, "NODE_NOT_FOUND", "节点不存在")
    except StaleGeneration as exc:
        return _error(409, "NODE_GENERATION_STALE", str(exc))
    except InvalidNodeState as exc:
        return _error(409, "INVALID_STATE_TRANSITION", str(exc))

    return JSONResponse(
        status_code=202,
        content=_node_body(row, active_sandboxes=_active_sandboxes(node_id)),
    )


# ==========================================
# 节点运维：cordon / drain / remove（D19）
# ==========================================

def _node_ops_error(exc: PlatformError):
    if isinstance(exc, NodeNotFound):
        return _error(404, "NODE_NOT_FOUND", str(exc))

    return _error(exc.http_status, exc.code, str(exc))


@router.post("/nodes/{node_id}/cordon", status_code=202)
def cordon_node(node_id: str):
    try:
        row = platform_ops.cordon_node(node_id)
    except PlatformError as exc:
        return _node_ops_error(exc)

    return JSONResponse(
        status_code=202,
        content=_node_body(row, active_sandboxes=_active_sandboxes(node_id)),
    )


@router.post("/nodes/{node_id}/drain", status_code=202)
def drain_node(node_id: str):
    try:
        row = platform_ops.drain_node(node_id)
    except PlatformError as exc:
        return _node_ops_error(exc)

    return JSONResponse(
        status_code=202,
        content=_node_body(row, active_sandboxes=_active_sandboxes(node_id)),
    )


@router.post("/nodes/{node_id}/remove")
def remove_node(node_id: str):
    try:
        row = NodeRegistry().remove(node_id)
    except NodeBusy as exc:
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "retryable": True,
                    "request_id": None,
                }
            },
        )
    except PlatformError as exc:
        return _node_ops_error(exc)

    return JSONResponse(
        status_code=200,
        content=_node_body(row, active_sandboxes=_active_sandboxes(node_id)),
    )


# ==========================================
# 沙箱
# ==========================================

def _actor(request) -> str:
    """审计里的调用方标识：优先令牌指纹，其次来源主机。"""

    token = os.getenv("PERSONAL_AGENT_PLATFORM_TOKEN", "").strip()

    if token:
        return "tok:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]

    host = request.client.host if request.client else "unknown"

    return f"host:{host}"


def _audit(
    event: str,
    *,
    request,
    outcome: str = "ok",
    code: str | None = None,
    sandbox_id: str | None = None,
    exec_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """审计写入失败不能影响正常请求，只记日志。"""

    try:
        AuditLog().record(
            event,
            outcome=outcome,
            code=code,
            actor=_actor(request),
            sandbox_id=sandbox_id,
            exec_id=exec_id,
            detail=detail,
        )
    except Exception:  # noqa: BLE001 - 审计是旁路
        logger.exception("审计写入失败 event=%s", event)


@router.post("/sandboxes", status_code=201)
def create_sandbox(
    payload: CreateSandboxRequest,
    request: Request,
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key"
    ),
    tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
):
    try:
        sandbox = platform_ops.create_sandbox(
            idempotency_key=idempotency_key or "",
            tier=payload.tier,
            workspace_id=payload.workspace_id,
            ttl_seconds=payload.ttl_seconds,
            tenant_id=tenant_id or "default",
        )
    except PlatformError as exc:
        _audit(
            "sandbox.create",
            request=request,
            outcome="denied",
            code=exc.code,
        )
        return _error(
            exc.http_status,
            exc.code,
            str(exc),
            retryable=bool(getattr(exc, "retryable", False)),
        )

    _audit(
        "sandbox.create",
        request=request,
        sandbox_id=sandbox["id"],
        detail={"created": sandbox.get("created", True)},
    )

    return JSONResponse(status_code=201, content=_sandbox_body(sandbox))


@router.get("/sandboxes/{sandbox_id}")
def get_sandbox(sandbox_id: str):
    sandbox, error = _require_sandbox(sandbox_id)

    if error is not None:
        return error

    return _sandbox_body(sandbox)


@router.post("/sandboxes/{sandbox_id}/destroy")
def destroy_sandbox(sandbox_id: str, request: Request):
    try:
        reclaimed = platform_ops.destroy_sandbox(sandbox_id)
    except PlatformError as exc:
        _audit(
            "sandbox.destroy",
            request=request,
            outcome="denied",
            code=exc.code,
            sandbox_id=sandbox_id,
        )
        return _error(
            exc.http_status,
            exc.code,
            str(exc),
            retryable=bool(getattr(exc, "retryable", False)),
        )

    _audit("sandbox.destroy", request=request, sandbox_id=sandbox_id)

    return _sandbox_body(reclaimed)


# ==========================================
# exec
# ==========================================

@router.post("/sandboxes/{sandbox_id}/exec", status_code=202)
def create_exec(
    sandbox_id: str,
    payload: ExecRequest,
    request: Request,
    traceparent: str | None = Header(default=None, alias="traceparent"),
):
    command = payload.command

    try:
        row = platform_ops.submit_exec(
            sandbox_id,
            command=command,
            timeout_ms=payload.timeout_ms,
            max_output_bytes=payload.max_output_bytes,
            run_id=payload.run_id,
            step_id=payload.step_id,
            traceparent=traceparent,
        )
    except platform_ops.QueueFull:
        _audit(
            "exec.create",
            request=request,
            outcome="denied",
            code="QUEUE_FULL",
            sandbox_id=sandbox_id,
        )
        return _queue_full_error()
    except PlatformError as exc:
        _audit(
            "exec.create",
            request=request,
            outcome="denied",
            code=exc.code,
            sandbox_id=sandbox_id,
        )
        return _error(
            exc.http_status,
            exc.code,
            str(exc),
            retryable=bool(getattr(exc, "retryable", False)),
        )

    _audit(
        "exec.create",
        request=request,
        sandbox_id=sandbox_id,
        exec_id=row["id"],
        detail={"argv_bytes": len(" ".join(command))},
    )

    return JSONResponse(status_code=202, content=_exec_body(row))


@router.get("/exec/{exec_id}")
def get_exec(exec_id: str):
    try:
        row = ExecStore().get(exec_id)
    except KeyError:
        return _error(404, "EXEC_NOT_FOUND", "exec 不存在")

    return _exec_body(row)


@router.post("/exec/{exec_id}/cancel", status_code=202)
def cancel_exec(exec_id: str, request: Request):
    try:
        row, previous = platform_ops.cancel_exec(exec_id)
    except platform_ops.ExecNotFound:
        return _error(404, "EXEC_NOT_FOUND", "exec 不存在")
    except platform_ops.ExecNotCancellable:
        return _error(409, "EXEC_NOT_CANCELLABLE", "exec 已到不可取消的终态")
    except PlatformError as exc:
        return _error(exc.http_status, exc.code, str(exc))

    if previous == "queued":
        _audit(
            "exec.cancel",
            request=request,
            sandbox_id=row["sandbox_id"],
            exec_id=exec_id,
            detail={"was": "queued"},
        )
    else:
        _audit(
            "exec.cancel",
            request=request,
            sandbox_id=row["sandbox_id"],
            exec_id=exec_id,
        )

    # 返回完整资源：SDK 的 cancel() 会按 ExecResult 解析，缺字段会炸。
    return JSONResponse(status_code=202, content=_exec_body(row))


@router.get("/queue")
def queue_status():
    """D30.1：执行队列深度。

    **单进程视角** —— 多 worker 部署时每个进程各有自己的队列，这里只报
    当前进程看到的数字。
    """

    return platform_ops.queue_status()


@router.get("/audit")
def list_audit(
    limit: int = 100,
    event: str | None = None,
    outcome: str | None = None,
    since: str | None = None,
):
    """最近的平台操作审计（新的一般在前）；支持按事件 / 结果 / 时间过滤。"""

    if event is not None and event not in EVENTS:
        return _error(400, "INVALID_ARGUMENT", f"未知审计事件：{event}")

    if since is not None:
        try:
            datetime.fromisoformat(since)
        except ValueError:
            return _error(400, "INVALID_ARGUMENT", "since 必须是 ISO 8601 时间")

    return {
        "records": AuditLog().list(
            limit=limit,
            event=event,
            outcome=outcome,
            since=since,
        )
    }
