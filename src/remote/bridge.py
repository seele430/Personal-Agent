"""远程会话桥接：令牌通过守卫后，以**只读 owner 身份**访问白名单接口。

三条安全约束（写死在这里）：

1. **必须显式配置 owner**：没配就根本不安装桥接（不猜站长）；
2. **身份固定 member**：不给 owner 角色，避免远程端拿到管理写权限；
3. **只在远程模式装**：本机登录路径完全不受影响（桥接内部按标记分流）。
"""

from __future__ import annotations

from fastapi import Depends, Request


REMOTE_MARKER = "remote_owner"


def mark_owner(scope, owner_id: str) -> None:
    """守卫通过后调用，把 owner 写进 ASGI state。"""

    if not owner_id:
        return

    state = scope.setdefault("state", {})
    state[REMOTE_MARKER] = owner_id


def marked_owner(request: Request) -> str | None:
    value = getattr(request.state, REMOTE_MARKER, None)

    return value if isinstance(value, str) and value else None


def make_bridged_auth(original, *, bearer_scheme, get_auth_store):
    """返回一个替换 `get_current_user` 的依赖函数。"""

    def bridged(
        request: Request,
        credentials=Depends(bearer_scheme),
        store=Depends(get_auth_store),
    ) -> dict:
        owner_id = marked_owner(request)

        if owner_id:
            return {
                "id": owner_id,
                "username": "remote",
                "role": "member",
                "remote": True,
            }

        return original(credentials=credentials, store=store)

    return bridged


def install(app, original, *, bearer_scheme, get_auth_store) -> bool:
    """安装桥接；重复安装是幂等的。"""

    if app.dependency_overrides.get(original) is not None:
        return False

    app.dependency_overrides[original] = make_bridged_auth(
        original, bearer_scheme=bearer_scheme, get_auth_store=get_auth_store
    )

    return True
