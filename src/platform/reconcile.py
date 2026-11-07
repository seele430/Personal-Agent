"""D22：启动对账 —— 重启后把中断的执行与孤儿资源收敛到确定状态。

原则：

- 只处理**本平台自己拥有的资源**：``platform.db`` 里的行，以及沙箱根目录下
  以 ``sbx_`` 命名的目录；不按进程名清理任何东西。
- 中断的 exec 落 ``failed / INTERRUPTED_BY_RESTART``，**不写成 cancelled** ——
  我们并不知道进程树是否已经清干净，不能伪造。
- 工作区缺失的沙箱落 ``failed / workspace_missing``；``running`` 但已经没有
  活动 exec 的沙箱回到 ``idle``。

对账是幂等的：重复执行不会产生新的变化。
"""

from __future__ import annotations

import logging
import shutil

from contextlib import closing
from datetime import datetime, timezone

from src.platform import active_execs
from src.platform.exec_service import SANDBOX_ID_PATTERN
from src.platform.exec_store import (
    EXEC_FAILED,
    ExecStore,
)
from src.platform.paths import sandbox_root
from src.platform.sandbox_service import (
    SANDBOX_FAILED,
    SANDBOX_IDLE,
    SANDBOX_RECLAIMED,
    SANDBOX_RUNNING,
    SandboxService,
)

logger = logging.getLogger("personal_agent.platform.reconcile")

INTERRUPTED_ERROR = "INTERRUPTED_BY_RESTART"
WORKSPACE_MISSING = "workspace_missing"


def reconcile_startup(now: datetime | None = None) -> dict[str, int]:
    """执行一次启动对账；返回本次处理的数量。"""

    moment = now or datetime.now(timezone.utc)
    service = SandboxService()
    store = ExecStore()
    root = sandbox_root()

    summary = {
        "execs_failed": 0,
        "sandboxes_idle": 0,
        "sandboxes_failed": 0,
        "orphan_dirs_removed": 0,
    }

    _fail_interrupted_execs(store, summary)
    _reconcile_sandboxes(service, root, summary)
    _remove_orphan_dirs(service, root, summary)

    if any(summary.values()):
        logger.info(
            "startup reconcile: execs_failed=%(execs_failed)s "
            "sandboxes_idle=%(sandboxes_idle)s "
            "sandboxes_failed=%(sandboxes_failed)s "
            "orphan_dirs_removed=%(orphan_dirs_removed)s",
            summary,
        )

    return summary


def _fail_interrupted_execs(store: ExecStore, summary: dict[str, int]) -> None:
    """queued / running 的 exec 在新进程里不可能继续，落 failed。"""

    active = active_execs.snapshot()

    with closing(store.connect()) as conn:
        rows = conn.execute(
            """
            SELECT id FROM platform_execs
            WHERE state IN ('queued', 'running')
            """
        ).fetchall()

    for row in rows:
        exec_id = row["id"]

        if exec_id in active:
            # 本进程里有活动 token：这是真在跑，不是孤儿。
            continue

        wrote = store.finish(
            exec_id,
            state=EXEC_FAILED,
            exit_code=None,
            stdout="",
            stderr="服务重启，执行中断",
            truncated=False,
            error_code=INTERRUPTED_ERROR,
            result_code="EXEC_FAILED",
        )

        if not wrote:
            # 查询与落终态之间可能被别的路径先写了：只统计真正迁移的数量。
            continue

        summary["execs_failed"] += 1


def _reconcile_sandboxes(
    service: SandboxService, root, summary: dict[str, int]
) -> None:
    with closing(service.connect()) as conn:
        rows = conn.execute(
            """
            SELECT id, state FROM agent_sandboxes
            WHERE state IN ('created', 'running', 'idle')
            ORDER BY created_at
            """
        ).fetchall()

    for row in rows:
        sandbox_id = row["id"]
        workspace = root / sandbox_id / "workspace"

        if not workspace.is_dir():
            # 工作区没了：这个沙箱已经不能执行任何东西。
            service.transition(
                sandbox_id, SANDBOX_FAILED, reason=WORKSPACE_MISSING
            )
            summary["sandboxes_failed"] += 1
            continue

        if row["state"] == SANDBOX_RUNNING:
            # 重启后本进程没有任何活动 exec，running 是过期状态。
            service.transition(sandbox_id, SANDBOX_IDLE)
            summary["sandboxes_idle"] += 1


def _remove_orphan_dirs(
    service: SandboxService, root, summary: dict[str, int]
) -> None:
    """目录在、数据库没有对应行：崩溃在 mkdir 与 commit 之间留下的孤儿。"""

    if not root.is_dir():
        return

    with closing(service.connect()) as conn:
        known = {
            row[0]
            for row in conn.execute("SELECT id FROM agent_sandboxes").fetchall()
        }

    for path in root.iterdir():
        if not path.is_dir():
            continue

        # 只清理本平台命名规则的目录，别的文件一律不碰。
        if SANDBOX_ID_PATTERN.fullmatch(path.name) is None:
            continue

        if path.name in known:
            continue

        shutil.rmtree(path, ignore_errors=True)
        summary["orphan_dirs_removed"] += 1
