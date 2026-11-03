"""D15：沙箱创建与幂等（idempotency key）。

契约见 ``docs/platform-contract.md`` 第 3.1 节：

- 同 key + 同规范化请求体 → 同一 ``sandbox_id``，不重复分配工作区；
- 同 key + 不同请求体 → 409 ``IDEMPOTENCY_CONFLICT``；
- 并发请求只允许一个创建者；
- 控制面重启后行为不变。

实现要点：**SQLite 唯一约束 + 同一事务**，不靠进程内字典、不靠「先查再插」。
创建路径用 ``BEGIN IMMEDIATE`` 先拿写锁，竞争者在锁释放后读到已存在的记录。
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import uuid

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src import config
from src.platform.paths import sandbox_root

IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

DEFAULT_TIER = "tier1"
SUPPORTED_TIERS = ("tier1",)
DEFAULT_TTL_SECONDS = 600
MAX_TTL_SECONDS = 86_400

SANDBOX_CREATED = "created"
SANDBOX_RECLAIMED = "reclaimed"


class PlatformError(RuntimeError):
    """带稳定错误码的平台异常；HTTP 层直接映射 code 与 status。"""

    code = "INTERNAL_ERROR"
    http_status = 500


class InvalidIdempotencyKey(PlatformError):
    code = "INVALID_IDEMPOTENCY_KEY"
    http_status = 400


class InvalidArgument(PlatformError):
    code = "INVALID_ARGUMENT"
    http_status = 400


class IdempotencyConflict(PlatformError):
    code = "IDEMPOTENCY_CONFLICT"
    http_status = 409


class SandboxNotFound(PlatformError):
    code = "SANDBOX_NOT_FOUND"
    http_status = 404


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_platform_schema(conn: sqlite3.Connection) -> None:
    """平台控制面表；只建不删，可重复执行。"""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_sandboxes (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            tier TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('created', 'reclaimed')),
            ttl_seconds INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            reclaimed_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sandbox_idempotency (
            key TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            sandbox_id TEXT NOT NULL REFERENCES agent_sandboxes(id),
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sandboxes_state
            ON agent_sandboxes(state)
        """
    )

    # D16：回收原因（manual / ttl_expired / reaper）。旧库补列，幂等。
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(agent_sandboxes)")
    }

    if "reclaim_reason" not in columns:
        conn.execute("ALTER TABLE agent_sandboxes ADD COLUMN reclaim_reason TEXT")


class SandboxService:
    def __init__(
        self,
        db_path: str | Path | None = None,
        root: str | Path | None = None,
    ):
        self.db_path = Path(db_path or config.paths.platform_db)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.root = Path(root) if root is not None else sandbox_root()

        with closing(self.connect()) as conn, conn:
            init_platform_schema(conn)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        idempotency_key: str,
        tier: str = DEFAULT_TIER,
        workspace_id: str | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> dict[str, Any]:
        """幂等创建沙箱；返回的记录里 ``created`` 表示本次是否真的新建。"""

        if (
            not isinstance(idempotency_key, str)
            or IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key) is None
        ):
            raise InvalidIdempotencyKey(
                "idempotency key 缺失或格式非法：1—128 位，仅允许字母数字与 _.-:"
            )

        if tier not in SUPPORTED_TIERS:
            raise InvalidArgument(f"tier 只支持 {SUPPORTED_TIERS}")

        if (
            type(ttl_seconds) is not int
            or not 1 <= ttl_seconds <= MAX_TTL_SECONDS
        ):
            raise InvalidArgument("ttl_seconds 必须是 1—86400 的整数")

        if workspace_id is None:
            # 省略值必须规范化成**确定性**默认值，否则同一个 key 的两次重试
            # 会算出不同指纹，误判成 409。
            workspace_id = f"ws_{idempotency_key}"

        if not isinstance(workspace_id, str) or not workspace_id.strip():
            raise InvalidArgument("workspace_id 必须是非空字符串")

        fingerprint = self._fingerprint(tier, workspace_id, ttl_seconds)

        for attempt in range(2):
            try:
                return self._create_once(
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                    tier=tier,
                    workspace_id=workspace_id,
                    ttl_seconds=ttl_seconds,
                )
            except sqlite3.IntegrityError:
                # 并发插入撞唯一约束：说明别人刚建好，重读一次即可。
                if attempt == 1:
                    raise

        raise PlatformError("沙箱创建失败")

    def _create_once(
        self,
        *,
        idempotency_key: str,
        fingerprint: str,
        tier: str,
        workspace_id: str,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")

            try:
                row = conn.execute(
                    "SELECT * FROM sandbox_idempotency WHERE key = ?",
                    (idempotency_key,),
                ).fetchone()

                if row is not None:
                    if row["fingerprint"] != fingerprint:
                        raise IdempotencyConflict(
                            "同一 idempotency key 不能对应不同请求体"
                        )

                    sandbox = self._row(conn, row["sandbox_id"])
                    sandbox["created"] = False
                    conn.commit()
                    return sandbox

                sandbox_id = f"sbx_{uuid.uuid4().hex}"
                workspace = self.root / sandbox_id / "workspace"
                workspace.mkdir(parents=True, exist_ok=False)

                created_at = datetime.now(timezone.utc)
                expires_at = created_at + timedelta(seconds=ttl_seconds)

                conn.execute(
                    """
                    INSERT INTO agent_sandboxes(
                        id, workspace_id, tier, state, ttl_seconds,
                        created_at, expires_at
                    ) VALUES (?, ?, ?, 'created', ?, ?, ?)
                    """,
                    (
                        sandbox_id,
                        workspace_id,
                        tier,
                        ttl_seconds,
                        created_at.isoformat(),
                        expires_at.isoformat(),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO sandbox_idempotency(
                        key, fingerprint, sandbox_id, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        idempotency_key,
                        fingerprint,
                        sandbox_id,
                        created_at.isoformat(),
                    ),
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

        sandbox = {
            "id": sandbox_id,
            "workspace_id": workspace_id,
            "tier": tier,
            "state": SANDBOX_CREATED,
            "ttl_seconds": ttl_seconds,
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "reclaimed_at": None,
            "workspace": str(workspace),
        }
        sandbox["created"] = True
        return sandbox

    # ------------------------------------------------------------------
    # 查询与回收
    # ------------------------------------------------------------------

    def get(self, sandbox_id: str) -> dict[str, Any]:
        with closing(self.connect()) as conn:
            return self._row(conn, sandbox_id)

    def reclaim(self, sandbox_id: str, *, reason: str = "manual") -> dict[str, Any]:
        """回收工作区并把状态置为 reclaimed；重复调用幂等。

        ``reason`` 会写进 tombstone：``manual``（用户/API 主动销毁）、
        ``ttl_expired``（reaper 到期回收）。
        """

        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")

            try:
                row = conn.execute(
                    "SELECT * FROM agent_sandboxes WHERE id = ?", (sandbox_id,)
                ).fetchone()

                if row is None:
                    raise SandboxNotFound(sandbox_id)

                if row["state"] != SANDBOX_RECLAIMED:
                    conn.execute(
                        """
                        UPDATE agent_sandboxes
                        SET state = 'reclaimed', reclaimed_at = ?,
                            reclaim_reason = ?
                        WHERE id = ?
                        """,
                        (utc_now(), reason, sandbox_id),
                    )

                conn.commit()
            except BaseException:
                conn.rollback()
                raise

        shutil.rmtree(self.root / sandbox_id, ignore_errors=True)

        return self.get(sandbox_id)

    def list_expired(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """还没回收但已经过期的沙箱。"""

        moment = now or datetime.now(timezone.utc)

        with closing(self.connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_sandboxes
                WHERE state = 'created' AND expires_at <= ?
                ORDER BY expires_at
                """,
                (moment.isoformat(),),
            ).fetchall()

        return [dict(row) for row in rows]

    def purge_tombstones(self, before: datetime | None = None) -> int:
        """删除回收时间早于 ``before`` 的 tombstone 及其幂等记录。

        契约要求两者「原子清除」：同一事务里先删幂等记录再删沙箱，
        避免出现「沙箱没了、key 还占着」的半状态。
        """

        moment = before or (datetime.now(timezone.utc) - timedelta(hours=24))

        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")

            try:
                rows = conn.execute(
                    """
                    SELECT id FROM agent_sandboxes
                    WHERE state = 'reclaimed' AND reclaimed_at <= ?
                    """,
                    (moment.isoformat(),),
                ).fetchall()

                ids = [row["id"] for row in rows]

                for sandbox_id in ids:
                    conn.execute(
                        "DELETE FROM sandbox_idempotency WHERE sandbox_id = ?",
                        (sandbox_id,),
                    )
                    conn.execute(
                        "DELETE FROM agent_sandboxes WHERE id = ?",
                        (sandbox_id,),
                    )

                conn.commit()
            except BaseException:
                conn.rollback()
                raise

        return len(ids)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _fingerprint(tier: str, workspace_id: str, ttl_seconds: int) -> str:
        import hashlib

        normalized = json.dumps(
            {
                "tier": tier,
                "workspace_id": workspace_id,
                "ttl_seconds": ttl_seconds,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _row(self, conn: sqlite3.Connection, sandbox_id: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM agent_sandboxes WHERE id = ?", (sandbox_id,)
        ).fetchone()

        if row is None:
            raise SandboxNotFound(sandbox_id)

        sandbox = dict(row)
        sandbox["workspace"] = str(
            self.root / sandbox["id"] / "workspace"
        )
        return sandbox
