"""数据生命周期（v1.3）：保留期清理，默认只预演。"""

from src.lifecycle.files import (
    DEFAULT_SUFFIXES,
    FileCandidate,
    FileLifecycleError,
    FilePlan,
    plan_files,
    purge_files,
)
from src.lifecycle.retention import (
    ALLOWED_TABLES,
    PurgePlan,
    PurgeTarget,
    RetentionError,
    RetentionPolicy,
    apply,
    plan,
    plan_table,
)


__all__ = [
    "ALLOWED_TABLES",
    "DEFAULT_SUFFIXES",
    "FileCandidate",
    "FileLifecycleError",
    "FilePlan",
    "plan_files",
    "purge_files",
    "PurgePlan",
    "PurgeTarget",
    "RetentionError",
    "RetentionPolicy",
    "apply",
    "plan",
    "plan_table",
]
