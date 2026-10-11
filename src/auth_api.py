
"""Personal Agent 登录认证接口。"""

from functools import lru_cache
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
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


# 数据库路径：项目根目录/auth_users.db
BASE_DIR = Path(__file__).resolve().parent.parent
AUTH_DB_PATH = BASE_DIR / "auth_users.db"

router = APIRouter(
    prefix="/auth",
    tags=["用户认证"],
)

bearer_scheme = HTTPBearer(
    auto_error=False
)


# ======================================
# 数据库依赖
# ======================================

@lru_cache(maxsize=1)
def get_auth_store() -> AuthStore:
    return AuthStore(AUTH_DB_PATH)


# ======================================
# 请求格式
# ======================================

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
# 获取当前登录用户
# ======================================

def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(
        bearer_scheme
    ),
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

    token = credentials.credentials

    try:
        user = store.session_user(token)
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
    response: Response,
    store: AuthStore = Depends(get_auth_store),
):
    response.headers["Cache-Control"] = "no-store"

    try:
        token = store.login(
            request.username,
            request.password,
        )

    except (AuthError, AuthForbidden):
        raise HTTPException(
            status_code=401,
            detail="用户名或密码不正确",
        ) from None

    user = store.session_user(token)

    if user is None:
        raise HTTPException(
            status_code=500,
            detail="登录状态创建失败",
        )

    return {
        "token": token,
        "token_type": "bearer",
        "user": user,
    }


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
    credentials: HTTPAuthorizationCredentials = Depends(
        bearer_scheme
    ),
    user: dict = Depends(get_current_user),
    store: AuthStore = Depends(get_auth_store),
):
    response.headers["Cache-Control"] = "no-store"

    store.logout(credentials.credentials)

    return {
        "status": "ok",
    }