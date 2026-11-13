"""工作区守卫：单写者租约 + 内容快照。

两条明确的行为：

1. **同一工作区只允许一个写者**：他人持有未过期租约时，`acquire` 抛 `WorkspaceBusy`；
2. **快照只读**：算 sha256 前缀，不修改任何文件；大文件跳过并记录。
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_TTL = 600
MAX_HASH_BYTES = 50 * 1024 * 1024
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "dist", "build"}


class GuardError(ValueError):
    """参数不合法。"""


class WorkspaceBusy(RuntimeError):
    """工作区已被他人持有。"""


class LeaseNotFound(KeyError):
    """没有租约。"""


@dataclass(frozen=True)
class Lease:
    workspace: str
    owner: str
    expires_at: str

    def to_dict(self) -> dict:
        return {
            "workspace": self.workspace,
            "owner": self.owner,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class SnapshotDiff:
    added: tuple[str, ...]
    changed: tuple[str, ...]
    removed: tuple[str, ...]

    @property
    def dirty(self) -> bool:
        return bool(self.added or self.changed or self.removed)

    def to_dict(self) -> dict:
        return {
            "added": list(self.added),
            "changed": list(self.changed),
            "removed": list(self.removed),
            "dirty": self.dirty,
        }


class WorkspaceGuard:
    def __init__(self, db_path: str | Path, workspace: str | Path):
        self.db_path = Path(
            db_path or Path(tempfile.gettempdir()) / "pa-workspace-guard.db"
        )
        self.workspace = Path(workspace).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _init(self) -> None:
        with closing(sqlite3.connect(self.db_path, timeout=10)) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS workspace_leases ("
                "workspace TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at TEXT NOT NULL)"
            )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def acquire(self, owner: str, *, ttl_seconds: int = DEFAULT_TTL) -> Lease:
        if not owner.strip():
            raise GuardError("owner 不能为空")

        if not 1 <= ttl_seconds <= 86_400:
            raise GuardError("ttl_seconds 必须在 1..86400")

        key = str(self.workspace)
        now = self._now()
        expires = now + timedelta(seconds=ttl_seconds)

        with closing(sqlite3.connect(self.db_path, timeout=10)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner, expires_at FROM workspace_leases WHERE workspace=?",
                (key,),
            ).fetchone()

            if row is not None:
                current_owner, current_expiry = row

                if current_owner != owner:
                    try:
                        expiry = datetime.fromisoformat(current_expiry)

                    except ValueError:
                        expiry = now

                    if expiry > now:
                        raise WorkspaceBusy(
                            f"工作区被 {current_owner} 持有，到期 {current_expiry}"
                        )

            connection.execute(
                "INSERT INTO workspace_leases (workspace, owner, expires_at) VALUES (?,?,?) "
                "ON CONFLICT(workspace) DO UPDATE SET owner=excluded.owner, "
                "expires_at=excluded.expires_at",
                (key, owner, expires.isoformat()),
            )

        return Lease(workspace=key, owner=owner, expires_at=expires.isoformat())

    def release(self, owner: str) -> bool:
        with closing(sqlite3.connect(self.db_path, timeout=10)) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM workspace_leases WHERE workspace=? AND owner=?",
                (str(self.workspace), owner),
            )

            return cursor.rowcount > 0

    def current(self) -> Lease | None:
        with closing(sqlite3.connect(self.db_path, timeout=10)) as connection:
            row = connection.execute(
                "SELECT owner, expires_at FROM workspace_leases WHERE workspace=?",
                (str(self.workspace),),
            ).fetchone()

        if row is None:
            return None

        return Lease(workspace=str(self.workspace), owner=row[0], expires_at=row[1])

    def snapshot(self) -> dict[str, str]:
        """返回 {相对路径: sha256 前 16 位}；大文件跳过。"""

        result: dict[str, str] = {}

        for path in sorted(self.workspace.rglob("*")):
            if not path.is_file():
                continue

            if any(part in SKIP_DIRS for part in path.parts):
                continue

            try:
                if path.stat().st_size > MAX_HASH_BYTES:
                    result[path.relative_to(self.workspace).as_posix()] = "skipped"
                    continue

                digest = hashlib.sha256()

                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1_048_576), b""):
                        digest.update(chunk)

                result[path.relative_to(self.workspace).as_posix()] = (
                    digest.hexdigest()[:16]
                )

            except OSError:
                continue

        return result

    @staticmethod
    def diff(before: dict[str, str], after: dict[str, str]) -> SnapshotDiff:
        before_keys = set(before)
        after_keys = set(after)

        return SnapshotDiff(
            added=tuple(sorted(after_keys - before_keys)),
            changed=tuple(
                sorted(
                    key
                    for key in before_keys & after_keys
                    if before[key] != after[key]
                )
            ),
            removed=tuple(sorted(before_keys - after_keys)),
        )
