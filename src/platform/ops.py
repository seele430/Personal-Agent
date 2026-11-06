"""平台写操作的唯一业务入口：``/v1`` 与控制台共用同一套实现。

只做业务，不碰 HTTP：

- 参数校验、沙箱状态机、节点调度、执行队列与执行线程都在这里；
- 失败抛 :class:`PlatformError` 子类，调用方按 ``code`` / ``http_status``
  映射成响应，并自己写审计（记录操作人与来源）；
- 唯一的例外是排队超时（``exec.timeout``）—— 那是队列自己的副作用。

为什么执行线程也搬进来了：``ExecQueue`` 只有装好 starter 才会真正跑；
控制台如果自己拿 ``get_queue()`` 提交，exec 会永远停在 queued。
"""

from __future__ import annotations

import logging
import os
import threading

from datetime import datetime, timezone
from typing import Any

from src.core.run_control import CancellationToken
from src.platform import active_execs
from src.platform.audit import AuditLog
from src.platform.exec_queue import QueuedExec, get_queue
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
from src.platform.node_registry import NodeRegistry
from src.platform.sandbox_service import (
    InvalidArgument,
    InvalidTransition,
    PlatformError,
    QuotaExceeded,
    SandboxNotFound,
    SandboxService,
)
from src.platform.trace_context import is_valid, new_traceparent

logger = logging.getLogger("personal_agent.platform.ops")

MAX_TIMEOUT_MS = 300_000
MAX_OUTPUT_BYTES = 1_048_576
RUNNABLE_SANDBOX_STATES = ("created", "running", "idle")


class NoReadyNode(PlatformError):
    code = "NO_READY_NODE"
    http_status = 429
    retryable = True


class QueueFull(PlatformError):
    code = "QUEUE_FULL"
    http_status = 429
    retryable = True


class ExecNotFound(PlatformError):
    code = "EXEC_NOT_FOUND"
    http_status = 404


class ExecNotCancellable(PlatformError):
    code = "EXEC_NOT_CANCELLABLE"
    http_status = 409


class SandboxGone(PlatformError):
    code = "SANDBOX_GONE"
    http_status = 410


# ----------------------------------------------------------------------
# 沙箱
# ----------------------------------------------------------------------


def _sandbox_expired(sandbox: dict[str, Any]) -> bool:
    """沙箱是否已经越过 TTL（惰性兜底：reaper 没跑到时访问自身也会触发）。"""

    expires_at = sandbox.get("expires_at")

    if not expires_at:
        return False

    try:
        deadline = datetime.fromisoformat(expires_at)
    except ValueError:
        return False

    return datetime.now(timezone.utc) >= deadline


def require_sandbox(
    sandbox_id: str,
    *,
    allow_reclaimed: bool = False,
) -> dict[str, Any]:
    service = SandboxService()

    try:
        sandbox = service.get(sandbox_id)
    except SandboxNotFound:
        raise SandboxNotFound("沙箱不存在") from None

    if sandbox["state"] != "reclaimed" and _sandbox_expired(sandbox):
        active_execs.cancel_for_sandbox(sandbox_id)
        sandbox = service.reclaim(sandbox_id, reason="ttl_expired")

    if sandbox["state"] == "reclaimed" and not allow_reclaimed:
        raise SandboxGone("沙箱已回收")

    return sandbox


def create_sandbox(
    *,
    idempotency_key: str = "",
    tier: str = "tier1",
    workspace_id: str | None = None,
    ttl_seconds: int = 600,
    tenant_id: str = "default",
) -> dict[str, Any]:
    """挑一个 least-loaded 节点建沙箱；没有可用节点时抛 429 类错误。"""

    registry = NodeRegistry()
    registry.ensure_default_node()

    node = registry.pick_node()

    if node is None:
        if registry.has_full_nodes():
            raise QuotaExceeded("所有可用节点都已达到容量上限")

        raise NoReadyNode("没有可接收新沙箱的节点（cordon / drain / 租约过期）")

    return SandboxService().create(
        idempotency_key=idempotency_key or "",
        tier=tier,
        workspace_id=workspace_id,
        ttl_seconds=ttl_seconds,
        node_id=node["node_id"],
        tenant_id=tenant_id or "default",
    )


def destroy_sandbox(sandbox_id: str) -> dict[str, Any]:
    require_sandbox(sandbox_id, allow_reclaimed=True)

    return SandboxService().reclaim(sandbox_id, reason="manual")


def sandbox_detail(sandbox_id: str) -> dict[str, Any]:
    return require_sandbox(sandbox_id)


# ----------------------------------------------------------------------
# 执行
# ----------------------------------------------------------------------


def submit_exec(
    sandbox_id: str,
    *,
    command: list[str],
    timeout_ms: int,
    max_output_bytes: int,
    run_id: str | None = None,
    step_id: str | None = None,
    traceparent: str | None = None,
) -> dict[str, Any]:
    """创建 exec 并交给队列；返回创建出来的行。"""

    sandbox = require_sandbox(sandbox_id)

    if sandbox["state"] not in RUNNABLE_SANDBOX_STATES:
        raise InvalidTransition(
            f"沙箱处于 {sandbox['state']}，不接受新的 exec"
        )

    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) and item for item in command)
    ):
        raise InvalidArgument("command 必须是非空字符串数组")

    if not 1 <= int(timeout_ms) <= MAX_TIMEOUT_MS:
        raise InvalidArgument(f"timeout_ms 必须在 1—{MAX_TIMEOUT_MS}")

    if not 1 <= int(max_output_bytes) <= MAX_OUTPUT_BYTES:
        raise InvalidArgument(
            f"max_output_bytes 必须在 1—{MAX_OUTPUT_BYTES}"
        )

    if not is_valid(traceparent):
        traceparent = new_traceparent()

    queue = exec_queue()

    if not queue.accepting():
        raise QueueFull("执行队列已满，请稍后重试")

    row = ExecStore().create(
        sandbox_id,
        argv=command,
        run_id=run_id,
        step_id=step_id,
        timeout_ms=timeout_ms,
        max_output_bytes=max_output_bytes,
        traceparent=traceparent,
    )

    outcome = queue.submit(
        QueuedExec(
            exec_id=row["id"],
            sandbox_id=sandbox_id,
            argv=command,
            timeout_ms=timeout_ms,
            max_output_bytes=max_output_bytes,
            traceparent=traceparent,
        )
    )

    if outcome == "full":
        # 预检之后被别人抢了位置：这条已经建出来的 exec 落失败。
        ExecStore().finish(
            row["id"],
            state=EXEC_FAILED,
            exit_code=None,
            stdout="",
            stderr="执行队列已满",
            truncated=False,
            error_code="QUEUE_FULL",
            result_code="EXEC_FAILED",
        )
        raise QueueFull("执行队列已满，请稍后重试")

    return row


def exec_detail(exec_id: str) -> dict[str, Any]:
    try:
        return ExecStore().get(exec_id)
    except KeyError:
        raise ExecNotFound("exec 不存在") from None


def cancel_exec(exec_id: str) -> tuple[dict[str, Any], str]:
    """幂等取消：已取消返回当前状态；已终态抛 409。

    返回 ``(行, 取消前的状态)``，调用方用它写审计。
    """

    row = exec_detail(exec_id)
    previous = row["state"]

    if previous == STORE_CANCELLED:
        return row, previous

    if previous in TERMINAL_EXEC_STATES:
        raise ExecNotCancellable("exec 已到不可取消的终态")

    if previous == "queued":
        # 排队中的 exec 从未起过进程：摘队列后直接落终态。
        # worker 可能正处在「已出队、还没置 running」的窗口里，顺手取消 token。
        exec_queue().cancel(exec_id)
        active_execs.cancel(exec_id)
        ExecStore().finish(
            exec_id,
            state=EXEC_CANCELLED,
            exit_code=None,
            stdout="",
            stderr="",
            truncated=False,
            error_code="EXEC_CANCELLED",
            result_code="EXEC_CANCELLED",
        )

        return ExecStore().get(exec_id), previous

    active_execs.cancel(exec_id)

    return row, previous


# ----------------------------------------------------------------------
# 节点
# ----------------------------------------------------------------------


def cordon_node(node_id: str) -> dict[str, Any]:
    return NodeRegistry().cordon(node_id)


def drain_node(node_id: str) -> dict[str, Any]:
    return NodeRegistry().drain(node_id)


def remove_node(node_id: str) -> dict[str, Any]:
    return NodeRegistry().remove(node_id)


def node_detail(node_id: str) -> dict[str, Any]:
    return NodeRegistry().get(node_id)


# ----------------------------------------------------------------------
# 执行队列与 worker
# ----------------------------------------------------------------------


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


def run_exec(
    exec_id: str,
    sandbox_id: str,
    argv: list[str],
    timeout_ms: int,
    max_output_bytes: int,
    traceparent: str | None = None,
) -> None:
    store = ExecStore()
    token = CancellationToken()
    service = SandboxService()

    active_execs.register(exec_id, sandbox_id, token)

    try:
        # 出队后可能已经被取消（排队期间取消不需要杀进程）。
        if store.get(exec_id)["state"] in TERMINAL_EXEC_STATES:
            return

        # 有 exec 在跑 → 沙箱进入 running；被回收/失败时直接拒绝执行。
        service.begin_exec(sandbox_id)

        outcome = Tier1Executor().execute_command(
            argv,
            sandbox_id=sandbox_id,
            cancel_token=token,
            timeout_ms=timeout_ms,
            max_output_bytes=max_output_bytes,
            on_started=lambda _sandbox: store.start(exec_id),
            traceparent=traceparent,
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
        try:
            # 没有活动 exec 了 → running 回到 idle。
            if store.get(exec_id)["state"] in TERMINAL_EXEC_STATES:
                service.end_exec(
                    sandbox_id,
                    active_execs=store.count_active(sandbox_id),
                )
        except (KeyError, PlatformError):
            logger.exception("sandbox end_exec 失败 sandbox_id=%s", sandbox_id)
        finally:
            # 最后才从活动表摘掉：只要还在这里，就说明 worker 可能还在碰数据库。
            active_execs.unregister(exec_id)
            exec_queue().finish()


def spawn_exec(item) -> None:
    """队列放行一条 exec：起线程跑真实的 tier1 执行。"""

    threading.Thread(
        target=run_exec,
        args=(
            item.exec_id,
            item.sandbox_id,
            item.argv,
            item.timeout_ms,
            item.max_output_bytes,
            item.traceparent,
        ),
        daemon=True,
    ).start()


def expire_queued(item) -> None:
    """排队等太久的 exec：落 timed_out，不启动进程。"""

    store = ExecStore()

    try:
        row = store.get(item.exec_id)
    except KeyError:
        return

    if row["state"] != "queued":
        return

    store.finish(
        item.exec_id,
        state=EXEC_TIMED_OUT,
        exit_code=None,
        stdout="",
        stderr="排队等待超时",
        truncated=False,
        error_code="QUEUE_TIMEOUT",
        result_code="EXEC_TIMED_OUT",
    )

    try:
        AuditLog().record(
            "exec.timeout",
            outcome="timed_out",
            code="QUEUE_TIMEOUT",
            sandbox_id=item.sandbox_id,
            exec_id=item.exec_id,
        )
    except Exception:  # noqa: BLE001 - 审计是旁路
        logger.exception("审计写入失败 event=exec.timeout")


def exec_queue():
    """进程级队列；只在第一次调用时装启动器与过期处理。"""

    queue = get_queue()

    if queue._start is None:  # noqa: SLF001 - 单例只需要装一次启动器
        queue.start_with(spawn_exec)
        queue.expire_with(expire_queued)

    return queue


def queue_status() -> dict[str, int]:
    return exec_queue().snapshot()


def queue_retry_after() -> int:
    """队列满时给调用方的退避提示（秒）—— 是估计值，不是精确等待时间。"""

    raw = os.getenv("PERSONAL_AGENT_PLATFORM_QUEUE_RETRY_AFTER", "").strip()

    try:
        value = int(raw)
    except ValueError:
        value = 5

    return value if value >= 1 else 5
