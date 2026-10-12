"""Personal Agent 个人站内提醒查询接口。

只允许查询当前登录用户自己的到期提醒。
站内通知中心未正式开放时，不提供提醒数据。
"""

from fastapi import APIRouter, Depends, Response

from src.auth_api import get_current_user
from src.core import reminder_identity
from src.core.reminder_store import ReminderStore


router = APIRouter(tags=["个人提醒"])


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

    store = ReminderStore(
        reminder_identity.REMINDER_DB_PATH
    )

    # 如果是第一次使用，安全创建缺失的表。
    # 不删除或修改已有提醒记录。
    store.init_db()

    reminders = store.due_in_app(
        user_id=user_id,
    )

    return {
        "enabled": True,
        "reminders": reminders,
    }