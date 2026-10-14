"""Personal Agent 个人提醒接口。

所有接口只操作当前登录用户自己的提醒：
user_id 一律取自服务端会话，不接受前端传入。

提醒的创建仍然只发生在聊天工具调用里
（src/core/reminder_identity.create_reminder_for_current_user），
本模块负责读取、修改、取消和确认送达。
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from src.auth_api import get_current_user
from src.core import reminder_identity
from src.core.reminder_store import (
    ReminderNotEditable,
    ReminderNotFound,
    ReminderStore,
)


router = APIRouter(tags=["个人提醒"])

ReminderStatus = Literal["pending", "sent", "cancelled"]


def _open_store() -> ReminderStore:
    """按当前配置打开提醒库，并安全地补齐缺失的表。"""

    store = ReminderStore(
        reminder_identity.REMINDER_DB_PATH
    )

    # 第一次使用时创建缺失的表。
    # 不删除或修改已有提醒记录。
    store.init_db()

    return store


def _not_found() -> HTTPException:
    # 不属于自己和不存在返回同一个响应，
    # 避免别人用 ID 探测他人提醒是否存在。
    return HTTPException(
        status_code=404,
        detail="提醒不存在或无权访问",
    )


class ReminderUpdate(BaseModel):
    """允许修改的字段。

    两个字段都不传时由 store 抛出 422。
    """

    message: str | None = None
    minutes: int | None = None


@router.get("/reminders/in-app/due")
def get_due_in_app_reminders(
    response: Response,
    user: dict = Depends(get_current_user),
):
    # 私人提醒不得由浏览器或代理缓存。
    response.headers["Cache-Control"] = "no-store"

    # 功能开关保持由服务端控制。
    # 前端不能自行决定是否启用。
    if not reminder_identity.IN_APP_DELIVERY_ENABLED:
        return {
            "enabled": False,
            "reminders": [],
        }

    # 使用真实登录会话里的身份。
    # 这里不接收前端传入的 user_id。
    user_id = user["id"]

    store = _open_store()

    reminders = store.due_in_app(
        user_id=user_id,
    )

    return {
        "enabled": True,
        "reminders": reminders,
    }


@router.get("/reminders")
def list_reminders(
    response: Response,
    status: ReminderStatus | None = None,
    user: dict = Depends(get_current_user),
):
    """列出当前用户自己的提醒。"""

    response.headers["Cache-Control"] = "no-store"

    try:
        reminders = _open_store().list_for_user(
            user_id=user["id"],
            status=status,
        )

    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail=str(error),
        ) from None

    return {
        "enabled": reminder_identity.IN_APP_DELIVERY_ENABLED,
        "reminders": reminders,
    }


@router.delete("/reminders/{reminder_id}")
def cancel_reminder(
    reminder_id: int,
    response: Response,
    user: dict = Depends(get_current_user),
):
    """取消（软删除）一条待发提醒。"""

    response.headers["Cache-Control"] = "no-store"

    try:
        result = _open_store().cancel(
            user_id=user["id"],
            reminder_id=reminder_id,
        )

    except ReminderNotFound:
        raise _not_found() from None

    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail=str(error),
        ) from None

    return result


@router.patch("/reminders/{reminder_id}")
def update_reminder(
    reminder_id: int,
    payload: ReminderUpdate,
    response: Response,
    user: dict = Depends(get_current_user),
):
    """修改一条待发提醒的内容或时间。"""

    response.headers["Cache-Control"] = "no-store"

    try:
        result = _open_store().update(
            user_id=user["id"],
            reminder_id=reminder_id,
            message=payload.message,
            minutes=payload.minutes,
        )

    except ReminderNotFound:
        raise _not_found() from None

    except ReminderNotEditable as error:
        raise HTTPException(
            status_code=409,
            detail=str(error),
        ) from None

    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail=str(error),
        ) from None

    return result


@router.post("/reminders/{reminder_id}/ack")
def acknowledge_reminder(
    reminder_id: int,
    response: Response,
    user: dict = Depends(get_current_user),
):
    """确认前端已经展示过一条到期的站内提醒。"""

    response.headers["Cache-Control"] = "no-store"

    try:
        result = _open_store().mark_delivered(
            user_id=user["id"],
            reminder_id=reminder_id,
        )

    except ReminderNotFound:
        raise _not_found() from None

    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail=str(error),
        ) from None

    return result
