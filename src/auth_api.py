
"""Personal Agent 登录认证接口，含持久化登录限流。"""

import hashlib
import sqlite3
import time

from contextlib import closing
from functools import lru_cache
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
)

from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBearer,
)

from pydantic import BaseModel, Field

from src.core.auth_store import (
    AuthError,
    AuthForbidden,
    AuthStore,
)

from src.config import paths


BASE_DIR = Path(__file__).resolve().parent.parent

LOGIN_WINDOW_SECONDS = 600
MAX_USER_ATTEMPTS = 8
MAX_IP_ATTEMPTS = 40

router = APIRouter(
    prefix="/auth",
    tags=["用户认证"],
)

bearer_scheme = HTTPBearer(
    auto_error=False
)


@lru_cache(maxsize=1)
def get_auth_store() -> AuthStore:
    return AuthStore(paths.auth_users_db)


class LoginRequest(BaseModel):
    username: str = Field(
        min_length=1,
        max_length=64,
    )

    password: str = Field(
        min_length=1,
        max_length=1024,
    )


# ======================================
# 登录频率限制
# ======================================

def _limit_key(
    category: str,
    value: str,
) -> str:
    """计数表使用摘要，不额外保存明文用户名或 IP。"""

    raw = f"{category}:{value}".encode("utf-8")

    return hashlib.sha256(raw).hexdigest()


def _login_limit_check(
    db_path: Path,
    username: str,
    ip_address: str,
) -> None:
    """
    原子检查和登记本次登录尝试。

    先检查次数，再进行密码哈希验证，
    避免无限错误登录耗尽服务器 CPU。
    """

    now = int(time.time())

    username_key = _limit_key(
        "user",
        username.strip().lower(),
    )

    ip_key = _limit_key(
        "ip",
        ip_address,
    )

    with closing(
        sqlite3.connect(db_path, timeout=10)
    ) as conn:

        with conn:
            conn.execute("BEGIN IMMEDIATE")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS
                auth_login_attempts (
                    key_hash TEXT PRIMARY KEY,
                    window_started INTEGER NOT NULL,
                    attempts INTEGER NOT NULL
                )
                """
            )

            checks = (
                (username_key, MAX_USER_ATTEMPTS),
                (ip_key, MAX_IP_ATTEMPTS),
            )

            updates = []
            blocked_seconds = 0

            for key_hash, maximum in checks:

                row = conn.execute(
                    """
                    SELECT
                        window_started,
                        attempts
                    FROM auth_login_attempts
                    WHERE key_hash = ?
                    """,
                    (key_hash,),
                ).fetchone()

                if (
                    row is None
                    or now - row[0] >= LOGIN_WINDOW_SECONDS
                ):
                    updates.append(
                        (key_hash, now, 1)
                    )

                elif row[1] >= maximum:

                    remaining = max(
                        1,
                        LOGIN_WINDOW_SECONDS
                        - (now - row[0]),
                    )

                    blocked_seconds = max(
                        blocked_seconds,
                        remaining,
                    )

                else:
                    updates.append(
                        (
                            key_hash,
                            row[0],
                            row[1] + 1,
                        )
                    )

            if not blocked_seconds:

                for (
                    key_hash,
                    started,
                    attempts,
                ) in updates:

                    conn.execute(
                        """
                        INSERT INTO auth_login_attempts (
                            key_hash,
                            window_started,
                            attempts
                        )
                        VALUES (?, ?, ?)

                        ON CONFLICT(key_hash)
                        DO UPDATE SET
                            window_started =
                                excluded.window_started,
                            attempts =
                                excluded.attempts
                        """,
                        (
                            key_hash,
                            started,
                            attempts,
                        ),
                    )

    if blocked_seconds:
        raise HTTPException(
            status_code=429,
            detail="登录尝试过于频繁，请稍后再试",
            headers={
                "Retry-After": str(blocked_seconds),
                "Cache-Control": "no-store",
            },
        )


def _clear_successful_username(
    db_path: Path,
    username: str,
) -> None:
    """成功登录后，只清除该用户名的计数。"""

    username_key = _limit_key(
        "user",
        username.strip().lower(),
    )

    with closing(
        sqlite3.connect(db_path, timeout=10)
    ) as conn:

        with conn:
            conn.execute(
                """
                DELETE FROM auth_login_attempts
                WHERE key_hash = ?
                """,
                (username_key,),
            )


# ======================================
# 获取当前登录用户
# ======================================

def get_current_user(
    credentials:
        HTTPAuthorizationCredentials | None
        = Depends(bearer_scheme),

    store: AuthStore = Depends(get_auth_store),
) -> dict:

    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="请先登录",
            headers={
                "WWW-Authenticate": "Bearer"
            },
        )

    try:
        user = store.session_user(
            credentials.credentials
        )

    except AuthError:
        user = None

    if user is None:
        raise HTTPException(
            status_code=401,
            detail="登录已失效，请重新登录",
            headers={
                "WWW-Authenticate": "Bearer"
            },
        )

    return user


# ======================================
# POST /auth/login
# ======================================

@router.post("/login")
def login(
    request: LoginRequest,
    http_request: Request,
    response: Response,
    store: AuthStore = Depends(get_auth_store),
):

    response.headers["Cache-Control"] = "no-store"

    ip_address = (
        http_request.client.host
        if http_request.client
        else "unknown"
    )

    # 只使用实际连接来源，不直接信任客户端
    # 自行提供的 X-Forwarded-For 头。
    _login_limit_check(
        store.db_path,
        request.username,
        ip_address,
    )

    try:
        token = store.login(
            request.username,
            request.password,
        )

    except (AuthError, AuthForbidden):
        raise HTTPException(
            status_code=401,
            detail="用户名或密码不正确",
            headers={
                "Cache-Control": "no-store"
            },
        ) from None

    user = store.session_user(token)

    if user is None:
        raise HTTPException(
            status_code=500,
            detail="登录状态创建失败",
        )

    _clear_successful_username(
        store.db_path,
        request.username,
    )

    return {
        "token": token,
        "token_type": "bearer",
        "user": user,
    }


# ======================================
# GET /auth/state —— 前端据此决定显示「登录」还是「首次初始化」
# ======================================

class BootstrapRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


@router.get("/state")
def auth_state(
    response: Response,
    store: AuthStore = Depends(get_auth_store),
):
    response.headers["Cache-Control"] = "no-store"

    initialized = store.has_users()

    return {
        "initialized": initialized,
        "registration_open": not initialized,
    }


# ======================================
# POST /auth/bootstrap —— 空库时创建站长并直接登录
# ======================================

@router.post("/bootstrap", status_code=201)
def bootstrap(
    request: BootstrapRequest,
    http_request: Request,
    response: Response,
    store: AuthStore = Depends(get_auth_store),
):
    response.headers["Cache-Control"] = "no-store"

    if store.has_users():
        raise HTTPException(
            status_code=409,
            detail="已经初始化过了，请直接登录",
        )

    ip_address = (
        http_request.client.host
        if http_request.client
        else "unknown"
    )

    _login_limit_check(
        store.db_path,
        request.username,
        ip_address,
    )

    try:
        store.bootstrap_owner(
            request.username,
            request.password,
        )

    except (AuthError, AuthForbidden) as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from None

    token = store.login(
        request.username,
        request.password,
    )

    user = store.session_user(token)

    if user is None:
        raise HTTPException(
            status_code=500,
            detail="初始化成功，但登录状态创建失败",
        )

    return {
        "token": token,
        "token_type": "bearer",
        "user": user,
    }


# ======================================
# POST /auth/reset-password —— 忘记密码（仅本机可用的自助重置）
# ======================================

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


class ResetPasswordRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


@router.post("/reset-password")
def reset_password(
    request: ResetPasswordRequest,
    http_request: Request,
    response: Response,
    store: AuthStore = Depends(get_auth_store),
):
    """本地版本没有邮箱找回，只能从本机重置。

    只接受回环地址：把服务暴露到非本机时，这个入口自动失效。
    """

    response.headers["Cache-Control"] = "no-store"

    host = (
        http_request.client.host
        if http_request.client
        else "unknown"
    )

    if host not in LOOPBACK_HOSTS:
        raise HTTPException(
            status_code=403,
            detail="忘记密码只能在运行服务的本机上操作",
        )

    _login_limit_check(
        store.db_path,
        request.username,
        host,
    )

    try:
        store.set_password(
            request.username,
            request.password,
        )

    except (AuthError, AuthForbidden) as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from None

    return {"ok": True}


# ======================================
# GET /auth/me
# ======================================

@router.get("/me")
def me(
    response: Response,
    user: dict = Depends(get_current_user),
):

    response.headers["Cache-Control"] = "no-store"

    return {
        "user": user,
    }


# ======================================
# POST /auth/logout
# ======================================

@router.post("/logout")
def logout(
    response: Response,

    credentials: HTTPAuthorizationCredentials
        = Depends(bearer_scheme),

    user: dict = Depends(get_current_user),

    store: AuthStore = Depends(get_auth_store),
):

    response.headers["Cache-Control"] = "no-store"

    store.logout(
        credentials.credentials
    )

    return {
        "status": "ok",
    }