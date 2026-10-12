"""经过认证的提醒身份上下文。

当前阶段只建立安全调用入口，不修改 agent.py 或聊天 API。

默认策略：
- owner：可以创建 ntfy 提醒
- 其他角色：在站内通知中心上线前，禁止创建提醒
- 没有认证上下文：禁止创建提醒

必须由服务端使用真实登录身份建立上下文，
不能直接使用浏览器传入的 user_id 或 role。
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from src.core.reminder_store import (
    REQUEST_ID_PATTERN,
    ROLES,
    ReminderStore,
)
from src.tools.reminder import DB_PATH


REMINDER_DB_PATH: Path = DB_PATH

# 网站内个人通知中心尚未上线。
# 在正式完成通知读取及权限测试前，保持 False。
IN_APP_DELIVERY_ENABLED = False


class ReminderDeliveryUnavailable(RuntimeError):
    """当前用户尚没有可以正常使用的通知渠道。"""


@dataclass(frozen=True)
class ReminderIdentity:
    user_id: str
    request_id: str
    role: str


_current_identity: ContextVar[
    ReminderIdentity | None
] = ContextVar(
    "personal_agent_reminder_identity",
    default=None,
)


@contextmanager
def bind_authenticated_reminder(
    *,
    user_id: str,
    request_id: str,
    role: str,
) -> Iterator[ReminderIdentity]:
    """为当前执行上下文绑定服务端认证身份。

    该函数本身不会查询登录会话。
    必须由已完成 get_current_user 验证的接口调用。
    """

    if (
        not isinstance(user_id, str)
        or not user_id.strip()
        or len(user_id) > 128
    ):
        raise ValueError("无效的用户 ID")

    if (
        not isinstance(request_id, str)
        or REQUEST_ID_PATTERN.fullmatch(request_id) is None
    ):
        raise ValueError("无效的 request_id")

    if role not in ROLES:
        raise ValueError("未知用户角色")

    identity = ReminderIdentity(
        user_id=user_id.strip(),
        request_id=request_id,
        role=role,
    )

    token = _current_identity.set(identity)

    try:
        yield identity
    finally:
        # 即使模型超时或抛出异常，也清理身份上下文。
        _current_identity.reset(token)


def create_reminder_for_current_user(
    message: str,
    minutes: int,
) -> dict:
    """按当前登录用户身份创建提醒。

    没有身份上下文时绝不回退到旧 create_reminder()。

    返回提醒记录信息，但不表示通知已经送达。
    """

    identity = _current_identity.get()

    if identity is None:
        raise PermissionError(
            "没有经过认证的提醒身份，拒绝创建提醒"
        )

    # 方案 A：
    # 站长沿用 ntfy；
    # 其他用户暂时等待网站内个人通知中心上线。
    if (
        identity.role != "owner"
        and not IN_APP_DELIVERY_ENABLED
    ):
        raise ReminderDeliveryUnavailable(
            "个人站内通知中心尚未开放，"
            "暂时无法为该用户创建提醒"
        )

    store = ReminderStore(REMINDER_DB_PATH)

    result = store.create(
        user_id=identity.user_id,
        request_id=identity.request_id,
        role=identity.role,
        message=message,
        minutes=minutes,
    )

    return {
        "id": result["id"],
        "delivery": result["delivery"],
        "remind_at": result["remind_at"],
        "created": result["created"],
        "legacy_reminder_id":
            result["legacy_reminder_id"],
    }