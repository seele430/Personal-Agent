
"""Personal Agent 邀请制用户存储层。

当前仅为本地服务模块，不对公网提供接口。
"""

import hashlib
import re
import secrets
import sqlite3
import time
import uuid

from contextlib import closing
from pathlib import Path


ROLE_LEVEL = {
    "member": 0,
    "trusted": 1,
    "admin": 2,
    "owner": 3,
}

USERNAME_RE = re.compile(
    r"^[a-z][a-z0-9_]{2,31}$"
)

PASSWORD_ROUNDS = 600_000


class AuthError(ValueError):
    pass


class AuthForbidden(PermissionError):
    pass


class AuthStore:

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

        self.db_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.init_db()

    # ======================================
    # SQLite 连接
    # ======================================

    def _connect(self):
        conn = sqlite3.connect(
            self.db_path,
            timeout=10,
        )

        conn.row_factory = sqlite3.Row

        conn.execute(
            "PRAGMA foreign_keys=ON"
        )

        return conn

    # ======================================
    # 初始化数据表
    # ======================================

    def init_db(self):
        with closing(self._connect()) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS auth_users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL,
                    role TEXT NOT NULL
                        CHECK(role IN (
                            'member',
                            'trusted',
                            'admin',
                            'owner'
                        )),
                    active INTEGER NOT NULL DEFAULT 1
                        CHECK(active IN (0,1)),
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS auth_invites (
                    token_hash TEXT PRIMARY KEY,
                    role TEXT NOT NULL
                        CHECK(role IN (
                            'member',
                            'trusted',
                            'admin'
                        )),
                    issued_by TEXT NOT NULL
                        REFERENCES auth_users(id),
                    expires_at INTEGER NOT NULL,
                    used_by TEXT REFERENCES auth_users(id),
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL
                        REFERENCES auth_users(id),
                    expires_at INTEGER NOT NULL,
                    revoked_at INTEGER,
                    created_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS
                    idx_auth_sessions_user
                ON auth_sessions(user_id);
                """
            )

    # ======================================
    # 用户名检查
    # ======================================

    @staticmethod
    def _username(value: str) -> str:
        if not isinstance(value, str):
            raise AuthError(
                "用户名格式不正确"
            )

        value = value.strip().lower()

        if not USERNAME_RE.fullmatch(value):
            raise AuthError(
                "用户名须为3-32位小写英文字母、"
                "数字或下划线，并以字母开头"
            )

        return value

    # ======================================
    # 密码处理
    # ======================================

    @staticmethod
    def _password(value: str) -> bytes:
        if not isinstance(value, str):
            raise AuthError(
                "密码不符合要求"
            )

        raw = value.encode("utf-8")

        if len(raw) < 12 or len(raw) > 1024:
            raise AuthError(
                "密码需要12-1024字节"
            )

        return raw

    @staticmethod
    def _digest(token: str) -> str:
        if not isinstance(token, str) or not token:
            raise AuthError(
                "令牌无效"
            )

        return hashlib.sha256(
            token.encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _hash_password(
        raw: bytes,
        salt: bytes,
    ) -> bytes:
        return hashlib.pbkdf2_hmac(
            "sha256",
            raw,
            salt,
            PASSWORD_ROUNDS,
        )

    @staticmethod
    def _public_user(row) -> dict:
        return {
            "id": row["id"],
            "username": row["username"],
            "role": row["role"],
            "active": bool(row["active"]),
        }

    # ======================================
    # 首次创建站长
    # ======================================

    def bootstrap_owner(
        self,
        username: str,
        password: str,
    ) -> dict:
        """
        只允许在空用户库中创建站长。

        必须从可信本地终端主动执行。
        禁止将本方法直接暴露为公共 API。
        """

        username = self._username(username)

        raw = self._password(password)

        salt = secrets.token_bytes(16)

        password_hash = self._hash_password(
            raw,
            salt,
        )

        user_id = uuid.uuid4().hex

        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")

            existing = conn.execute(
                "SELECT 1 FROM auth_users LIMIT 1"
            ).fetchone()

            if existing:
                raise AuthForbidden(
                    "数据库已初始化，不能重复创建站长"
                )

            conn.execute(
                """
                INSERT INTO auth_users (
                    id,
                    username,
                    salt,
                    password_hash,
                    role,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    username,
                    salt,
                    password_hash,
                    "owner",
                    int(time.time()),
                ),
            )

        return {
            "id": user_id,
            "username": username,
            "role": "owner",
            "active": True,
        }

    # ======================================
    # 检查邀请码管理权限
    # ======================================

    def _require_actor(
        self,
        conn,
        actor_id: str,
    ):
        row = conn.execute(
            """
            SELECT id, username, role, active
            FROM auth_users
            WHERE id = ?
            """,
            (actor_id,),
        ).fetchone()

        if (
            row is None
            or not row["active"]
            or ROLE_LEVEL[row["role"]] < 2
        ):
            raise AuthForbidden(
                "没有管理邀请码的权限"
            )

        return row

    # ======================================
    # 生成邀请码
    # ======================================

    def issue_invite(
        self,
        actor_id: str,
        role: str = "member",
        hours: int = 72,
    ) -> str:

        if role not in (
            "member",
            "trusted",
            "admin",
        ):
            raise AuthError(
                "不能创建该等级的邀请码"
            )

        if (
            type(hours) is not int
            or not 1 <= hours <= 720
        ):
            raise AuthError(
                "邀请码有效期为1-720小时"
            )

        token = secrets.token_urlsafe(32)
        now = int(time.time())

        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")

            actor = self._require_actor(
                conn,
                actor_id,
            )

            if (
                role == "admin"
                and actor["role"] != "owner"
            ):
                raise AuthForbidden(
                    "仅站长可以邀请管理员"
                )

            conn.execute(
                """
                INSERT INTO auth_invites (
                    token_hash,
                    role,
                    issued_by,
                    expires_at,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    self._digest(token),
                    role,
                    actor_id,
                    now + hours * 3600,
                    now,
                ),
            )

        return token

    # ======================================
    # 使用邀请码注册
    # ======================================

    def register(
        self,
        username: str,
        password: str,
        invite: str,
    ) -> dict:

        username = self._username(username)
        raw = self._password(password)

        salt = secrets.token_bytes(16)
        password_hash = self._hash_password(
            raw,
            salt,
        )

        token_hash = self._digest(invite)

        now = int(time.time())
        user_id = uuid.uuid4().hex

        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")

            row = conn.execute(
                """
                SELECT
                    v.role,
                    v.expires_at,
                    v.used_by,
                    u.active,
                    u.role AS issuer_role
                FROM auth_invites v
                JOIN auth_users u
                    ON v.issued_by = u.id
                WHERE v.token_hash = ?
                """,
                (token_hash,),
            ).fetchone()

            if (
                row is None
                or row["used_by"] is not None
                or row["expires_at"] <= now
                or not row["active"]
                or ROLE_LEVEL[row["issuer_role"]] < 2
                or (
                    row["role"] == "admin"
                    and row["issuer_role"] != "owner"
                )
            ):
                raise AuthError(
                    "邀请码无效、已使用或已过期"
                )

            try:
                conn.execute(
                    """
                    INSERT INTO auth_users (
                        id,
                        username,
                        salt,
                        password_hash,
                        role,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        username,
                        salt,
                        password_hash,
                        row["role"],
                        now,
                    ),
                )

            except sqlite3.IntegrityError as exc:
                raise AuthError(
                    "用户名已被使用"
                ) from exc

            conn.execute(
                """
                UPDATE auth_invites
                SET used_by = ?
                WHERE token_hash = ?
                  AND used_by IS NULL
                """,
                (user_id, token_hash),
            )

        return {
            "id": user_id,
            "username": username,
            "role": row["role"],
            "active": True,
        }

    # ======================================
    # 用户登录
    # ======================================

    def login(
        self,
        username: str,
        password: str,
        hours: int = 24,
    ) -> str:

        if (
            type(hours) is not int
            or not 1 <= hours <= 168
        ):
            raise AuthError(
                "会话有效期不正确"
            )

        try:
            username = self._username(username)
            raw = self._password(password)

        except AuthError:
            raise AuthError(
                "用户名或密码不正确"
            ) from None

        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT *
                FROM auth_users
                WHERE username = ?
                """,
                (username,),
            ).fetchone()

        if (
            row is None
            or not row["active"]
            or not secrets.compare_digest(
                self._hash_password(
                    raw,
                    row["salt"],
                ),
                row["password_hash"],
            )
        ):
            raise AuthError(
                "用户名或密码不正确"
            )

        token = secrets.token_urlsafe(32)
        now = int(time.time())

        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")

            active = conn.execute(
                """
                SELECT active
                FROM auth_users
                WHERE id = ?
                """,
                (row["id"],),
            ).fetchone()

            if (
                active is None
                or not active["active"]
            ):
                raise AuthError(
                    "用户名或密码不正确"
                )

            conn.execute(
                """
                INSERT INTO auth_sessions (
                    token_hash,
                    user_id,
                    expires_at,
                    created_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    self._digest(token),
                    row["id"],
                    now + hours * 3600,
                    now,
                ),
            )

        return token

    # ======================================
    # 会话身份查询
    # ======================================

    def session_user(
        self,
        token: str,
    ) -> dict | None:

        now = int(time.time())

        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT
                    u.id,
                    u.username,
                    u.role,
                    u.active
                FROM auth_sessions s
                JOIN auth_users u
                    ON s.user_id = u.id
                WHERE s.token_hash = ?
                  AND s.revoked_at IS NULL
                  AND s.expires_at > ?
                  AND u.active = 1
                """,
                (
                    self._digest(token),
                    now,
                ),
            ).fetchone()

        if row is None:
            return None

        return self._public_user(row)

    # ======================================
    # 权限检查
    # ======================================

    def require_role(
        self,
        token: str,
        minimum_role: str = "member",
    ) -> dict:

        if minimum_role not in ROLE_LEVEL:
            raise AuthError(
                "未知权限等级"
            )

        user = self.session_user(token)

        if (
            user is None
            or ROLE_LEVEL[user["role"]]
            < ROLE_LEVEL[minimum_role]
        ):
            raise AuthForbidden(
                "未登录或权限不足"
            )

        return user

    # ======================================
    # 退出登录
    # ======================================

    def logout(self, token: str) -> None:

        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                UPDATE auth_sessions
                SET revoked_at = ?
                WHERE token_hash = ?
                  AND revoked_at IS NULL
                """,
                (
                    int(time.time()),
                    self._digest(token),
                ),
            )