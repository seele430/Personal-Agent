"""跨 Agent 协作（v1.4）：能力快照、任务注册表、工作区守卫、独立验收。

真实 Provider / QQ 入站**不在本轮**（缺凭据），接口按"unknown 即拒绝"设计。
"""

from src.collab.capability import (
    ACTIONS,
    CapabilityError,
    CapabilitySnapshot,
    snapshot,
)
from src.collab.guard import (
    GuardError,
    Lease,
    SnapshotDiff,
    WorkspaceBusy,
    WorkspaceGuard,
)
from src.collab.registry import (
    STATES,
    ExternalTask,
    ExternalTaskRegistry,
    RegistryError,
    TaskNotFound,
)
from src.collab.verify import (
    VerifyCheck,
    VerifyError,
    VerifyResult,
    check_artifacts,
    check_digest,
    run_command,
    verify,
)


__all__ = [
    "ACTIONS",
    "STATES",
    "CapabilityError",
    "CapabilitySnapshot",
    "ExternalTask",
    "ExternalTaskRegistry",
    "GuardError",
    "Lease",
    "RegistryError",
    "SnapshotDiff",
    "TaskNotFound",
    "VerifyCheck",
    "VerifyError",
    "VerifyResult",
    "WorkspaceBusy",
    "WorkspaceGuard",
    "check_artifacts",
    "check_digest",
    "run_command",
    "snapshot",
    "verify",
]
