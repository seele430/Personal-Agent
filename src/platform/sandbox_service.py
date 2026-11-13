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
import os
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


class InvalidTransition(PlatformError):
    code = "INVALID_STATE_TRANSITION"
    http_status = 409


class QuotaExceeded(PlatformError):
    code = "CAPACITY_EXCEEDED"
    http_status = 429

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.retryable = True


SANDBOX_CREATED = "created"
SANDBOX_RUNNING = "running"
SANDBOX_IDLE = "idle"
SANDBOX_FAILED = "failed"
SANDBOX_RECLAIMED = "reclaimed"

SANDBOX_STATES = (
    SANDBOX_CREATED,
    SANDBOX_RUNNING,
    SANDBOX_IDLE,
    SANDBOX_FAILED,
    SANDBOX_RECLAIMED,
)

# 契约 4.2 的状态图；reclaimed 是终态，没有出边。
LEGAL_TRANSITIONS = {
    SANDBOX_CREATED: {SANDBOX_RUNNING, SANDBOX_FAILED, SANDBOX_RECLAIMED},
    SANDBOX_RUNNING: {SANDBOX_IDLE, SANDBOX_FAILED, SANDBOX_RECLAIMED},
    SANDBOX_IDLE: {SANDBOX_RUNNING, SANDBOX_FAILED, SANDBOX_RECLAIMED},
    SANDBOX_FAILED: {SANDBOX_RECLAIMED},
    SANDBOX_RECLAIMED: set(),
}

DEFAULT_NODE_PLACEHOLDER = "node_local"

# D21：每租户并发沙箱上限；单机单用户时等价于全局上限。
QUOTA_ENV = "PERSONAL_AGENT_SANDBOX_QUOTA"
DEFAULT_QUOTA = 8
MAX_QUOTA = 1000
DEFAULT_TENANT = "default"

_CREATE_SANDBOXES_SQL = """
CREATE TABLE IF NOT EXISTS agent_sandboxes (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    tier TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('created', 'running', 'idle', 'failed', 'reclaimed')
    ),
    ttl_seconds INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    reclaimed_at TEXT,
    reclaim_reason TEXT,
    node_id TEXT,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    fail_reason TEXT
)
"""


def sandbox_quota() -> int:
    raw = os.getenv(QUOTA_ENV, "").strip()

    if not raw:
        return DEFAULT_QUOTA

    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_QUOTA

    if value < 1:
        return DEFAULT_QUOTA

    return min(value, MAX_QUOTA)


GLOBAL_LIMIT_ENV = "PERSONAL_AGENT_SANDBOX_GLOBAL_LIMIT"
DEFAULT_GLOBAL_LIMIT = 8


def global_sandbox_limit() -> int:
    """单用户全局沙箱上限（所有租户合计）；0 表示不限额。"""

    raw = os.getenv(GLOBAL_LIMIT_ENV, "").strip()

    if not raw:
        return DEFAULT_GLOBAL_LIMIT

    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_GLOBAL_LIMIT

    return min(max(value, 0), MAX_QUOTA)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_platform_schema(conn: sqlite3.Connection) -> None:
    """平台控制面表；只建不删，可重复执行。"""

    _ensure_sandboxes_table(conn)

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


def _ensure_sandboxes_table(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("agent_sandboxes",),
    ).fetchone()

    if row is None:
        conn.execute(_CREATE_SANDBOXES_SQL)
        return

    sql = row[0] or ""

    if "'running'" in sql:
        # 表结构已经是 D20 版本：只需要补历史数据与新列。
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(agent_sandboxes)")
        }

        if "tenant_id" not in columns:
            conn.execute(
                """
                ALTER TABLE agent_sandboxes
                ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'
                """
            )

        # D22：failed 的终态原因（workspace_missing / runner_crashed / restart）。
        if "fail_reason" not in columns:
            conn.execute("ALTER TABLE agent_sandboxes ADD COLUMN fail_reason TEXT")

        conn.execute(
            """
            UPDATE agent_sandboxes SET node_id = ?
            WHERE node_id IS NULL
            """,
            (DEFAULT_NODE_PLACEHOLDER,),
        )
        return

    _rebuild_sandboxes_for_states(conn)


def _rebuild_sandboxes_for_states(conn: sqlite3.Connection) -> None:
    """D20：把只允许 created/reclaimed 的旧表重建为完整状态机。

    步骤刻意分成「建新表 → 拷贝 → 删旧表 → 改名」：SQLite 在 rename 时会重写
    其它表里指向被改名表的 REFERENCES，先改名会把 idempotency 的外键指到
    legacy 表上，所以必须先删后改名。
    """

    # PRAGMA foreign_keys 不能在事务里切换，所以先关、再开。
    conn.execute("PRAGMA foreign_keys = OFF")

    try:
        conn.execute("BEGIN")
        conn.execute(
            """
            CREATE TABLE agent_sandboxes__d20 (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                tier TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('created', 'running', 'idle', 'failed', 'reclaimed')
                ),
                ttl_seconds INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                reclaimed_at TEXT,
                reclaim_reason TEXT,
                node_id TEXT,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                fail_reason TEXT
            )
            """
        )

        # 旧库可能来自 D15（没有 reclaim_reason / node_id），按实际列拼 SELECT。
        existing = {
            row[1] for row in conn.execute("PRAGMA table_info(agent_sandboxes)")
        }
        reason_expr = (
            "reclaim_reason" if "reclaim_reason" in existing else "NULL"
        )
        node_expr = (
            "COALESCE(node_id, ?)"
            if "node_id" in existing
            else "?"
        )
        tenant_expr = (
            "COALESCE(tenant_id, 'default')"
            if "tenant_id" in existing
            else "'default'"
        )
        fail_expr = (
            "fail_reason" if "fail_reason" in existing else "NULL"
        )

        conn.execute(
            f"""
            INSERT INTO agent_sandboxes__d20(
                id, workspace_id, tier, state, ttl_seconds,
                created_at, expires_at, reclaimed_at, reclaim_reason,
                node_id, tenant_id, fail_reason
            )
            SELECT
                id, workspace_id, tier,
                CASE
                    WHEN state IN ('created', 'reclaimed') THEN state
                    ELSE 'created'
                END,
                ttl_seconds, created_at, expires_at, reclaimed_at,
                {reason_expr}, {node_expr}, {tenant_expr}, {fail_expr}
            FROM agent_sandboxes
            """,
            (DEFAULT_NODE_PLACEHOLDER,),
        )
        conn.execute("DROP TABLE agent_sandboxes")
        conn.execute(
            "ALTER TABLE agent_sandboxes__d20 RENAME TO agent_sandboxes"
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


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
        node_id: str | None = None,
        tenant_id: str = DEFAULT_TENANT,
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
                    node_id=node_id,
                    tenant_id=tenant_id,
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
        node_id: str | None,
        tenant_id: str,
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

                # D21：配额与容量在同一事务里检查，避免并发超分。
                # 修订版口径：单用户全局上限（所有租户合计）优先，租户配额是
                # 额外的一层，为将来多租户保留。
                global_limit = global_sandbox_limit()

                if global_limit:
                    total = conn.execute(
                        """
                        SELECT COUNT(*) FROM agent_sandboxes
                        WHERE state != 'reclaimed'
                        """
                    ).fetchone()[0]

                    if int(total) >= global_limit:
                        raise QuotaExceeded(
                            f"单用户全局沙箱上限已用完（{total}/{global_limit}）"
                        )

                used = conn.execute(
                    """
                    SELECT COUNT(*) FROM agent_sandboxes
                    WHERE tenant_id = ? AND state != 'reclaimed'
                    """,
                    (tenant_id,),
                ).fetchone()[0]

                if int(used) >= sandbox_quota():
                    raise QuotaExceeded(
                        f"租户 {tenant_id} 的沙箱并发额度已用完"
                        f"（{used}/{sandbox_quota()}）"
                    )

                if node_id is not None:
                    node = conn.execute(
                        "SELECT capacity FROM agent_nodes WHERE node_id = ?",
                        (node_id,),
                    ).fetchone()

                    if node is not None:
                        on_node = conn.execute(
                            """
                            SELECT COUNT(*) FROM agent_sandboxes
                            WHERE node_id = ? AND state != 'reclaimed'
                            """,
                            (node_id,),
                        ).fetchone()[0]

                        if int(on_node) >= int(node["capacity"]):
                            raise QuotaExceeded(
                                f"节点 {node_id} 容量已满"
                                f"（{on_node}/{node['capacity']}）"
                            )

                sandbox_id = f"sbx_{uuid.uuid4().hex}"
                workspace = self.root / sandbox_id / "workspace"
                workspace.mkdir(parents=True, exist_ok=False)

                created_at = datetime.now(timezone.utc)
                expires_at = created_at + timedelta(seconds=ttl_seconds)

                conn.execute(
                    """
                    INSERT INTO agent_sandboxes(
                        id, workspace_id, tier, state, ttl_seconds,
                        created_at, expires_at, node_id, tenant_id
                    ) VALUES (?, ?, ?, 'created', ?, ?, ?, ?, ?)
                    """,
                    (
                        sandbox_id,
                        workspace_id,
                        tier,
                        ttl_seconds,
                        created_at.isoformat(),
                        expires_at.isoformat(),
                        node_id,
                        tenant_id,
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
            "node_id": node_id,
            "tenant_id": tenant_id,
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

    def count_for_tenant(self, tenant_id: str = DEFAULT_TENANT) -> int:
        """该租户尚未回收的沙箱数（配额与观测用）。"""

        with closing(self.connect()) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM agent_sandboxes
                WHERE tenant_id = ? AND state != 'reclaimed'
                """,
                (tenant_id,),
            ).fetchone()

        return int(row[0])

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

                if row["state"] == SANDBOX_RECLAIMED:
                    conn.commit()
                else:
                    if SANDBOX_RECLAIMED not in LEGAL_TRANSITIONS.get(
                        row["state"], set()
                    ):
                        raise InvalidTransition(
                            f"{row['state']} → reclaimed 非法"
                        )

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

    def extend_ttl(self, sandbox_id: str, seconds: int) -> dict[str, Any]:
        """把到期时间推迟到 now+seconds；只延后不提前，重复调用幂等。

        长任务（编排 coding 1800 s）提交 exec 前由 ops 调用，保证
        reaper 不会在 exec 跑完前回收沙箱。上限仍是 MAX_TTL_SECONDS。
        """

        if (
            type(seconds) is not int
            or not 1 <= seconds <= MAX_TTL_SECONDS
        ):
            raise InvalidArgument(
                f"ttl 续期秒数必须是 1—{MAX_TTL_SECONDS} 的整数"
            )

        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")

            try:
                row = conn.execute(
                    "SELECT * FROM agent_sandboxes WHERE id = ?", (sandbox_id,)
                ).fetchone()

                if row is None:
                    raise SandboxNotFound(sandbox_id)

                if row["state"] == SANDBOX_RECLAIMED:
                    raise InvalidTransition("沙箱已回收，不能续期")

                current = datetime.fromisoformat(row["expires_at"])
                now = datetime.now(timezone.utc)
                target = now + timedelta(seconds=seconds)

                if target > current:
                    conn.execute(
                        """
                        UPDATE agent_sandboxes
                        SET expires_at = ?, ttl_seconds = ?
                        WHERE id = ?
                        """,
                        (target.isoformat(), seconds, sandbox_id),
                    )

                conn.commit()
            except BaseException:
                conn.rollback()
                raise

        return self.get(sandbox_id)

    # ------------------------------------------------------------------
    # D20 状态机
    # ------------------------------------------------------------------

    def transition(
        self,
        sandbox_id: str,
        to_state: str,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """按契约状态图迁移；非法迁移一律 409 ``INVALID_STATE_TRANSITION``。"""

        if to_state not in SANDBOX_STATES:
            raise ValueError(f"未知沙箱状态：{to_state}")

        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")

            try:
                row = conn.execute(
                    "SELECT * FROM agent_sandboxes WHERE id = ?", (sandbox_id,)
                ).fetchone()

                if row is None:
                    raise SandboxNotFound(sandbox_id)

                current = row["state"]

                if current == to_state:
                    conn.commit()
                    return self._row(conn, sandbox_id)

                if to_state not in LEGAL_TRANSITIONS.get(current, set()):
                    raise InvalidTransition(
                        f"{current} → {to_state} 非法"
                    )

                if to_state == SANDBOX_RECLAIMED:
                    conn.execute(
                        """
                        UPDATE agent_sandboxes
                        SET state = ?, reclaimed_at = ?, reclaim_reason = ?
                        WHERE id = ?
                        """,
                        (to_state, utc_now(), reason, sandbox_id),
                    )
                elif to_state == SANDBOX_FAILED:
                    conn.execute(
                        """
                        UPDATE agent_sandboxes
                        SET state = ?, fail_reason = ?
                        WHERE id = ?
                        """,
                        (to_state, reason, sandbox_id),
                    )
                else:
                    conn.execute(
                        "UPDATE agent_sandboxes SET state = ? WHERE id = ?",
                        (to_state, sandbox_id),
                    )

                conn.commit()
            except BaseException:
                conn.rollback()
                raise

        return self.get(sandbox_id)

    def begin_exec(self, sandbox_id: str) -> dict[str, Any]:
        """有 exec 开始跑：created/idle → running；已经在 running 说明是并发 exec。"""

        row = self.get(sandbox_id)

        if row["state"] in (SANDBOX_CREATED, SANDBOX_IDLE):
            return self.transition(sandbox_id, SANDBOX_RUNNING)

        if row["state"] == SANDBOX_RUNNING:
            return row

        raise InvalidTransition(
            f"{row['state']} 状态不接受新的 exec"
        )

    def end_exec(self, sandbox_id: str, *, active_execs: int) -> dict[str, Any]:
        """exec 结束：没有活动 exec 时 running → idle。"""

        row = self.get(sandbox_id)

        if row["state"] == SANDBOX_RUNNING and active_execs == 0:
            return self.transition(sandbox_id, SANDBOX_IDLE)

        return row

    def mark_failed(self, sandbox_id: str, reason: str) -> dict[str, Any]:
        return self.transition(sandbox_id, SANDBOX_FAILED, reason=reason)

    def count_active(self, node_id: str) -> int:
        """某节点上还没回收的沙箱数（created/running/idle/failed 都算）。"""

        with closing(self.connect()) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM agent_sandboxes
                WHERE state != 'reclaimed' AND node_id = ?
                """,
                (node_id,),
            ).fetchone()

        return int(row[0])

    def list_expired(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """还没回收但已经过期的沙箱。"""

        moment = now or datetime.now(timezone.utc)

        with closing(self.connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_sandboxes
                WHERE state != 'reclaimed' AND expires_at <= ?
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
