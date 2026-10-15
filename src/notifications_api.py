"""站内通知接口。

非站长的监控/提醒结果不会推到站长的手机，而是落在站内：
用户通过这里读取自己的待确认通知，确认后置为已读。

只读 + 确认，没有创建入口 —— 通知由任务引擎写入。
"""

from fastapi import APIRouter, Depends, HTTPException, Response

from src.auth_api import get_current_user
from src.core.task_engine import (
    NotificationNotFound,
    list_in_app_notifications,
    mark_notification_read,
)


router = APIRouter(tags=["站内通知"])


def _not_found() -> HTTPException:
    # 别人的通知与不存在的通知返回同一个响应，
    # 避免用通知 ID 探测他人数据。
    return HTTPException(
        status_code=404,
        detail="通知不存在或无权访问",
    )


@router.get("/notifications")
def list_notifications(
    response: Response,
    user: dict = Depends(get_current_user),
):
    """列出当前用户尚未确认的站内通知。"""

    response.headers["Cache-Control"] = "no-store"

    try:
        notifications = list_in_app_notifications(
            user["id"]
        )

    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail=str(error),
        ) from None

    return {
        "notifications": notifications,
    }


@router.post("/notifications/{notification_id}/ack")
def acknowledge_notification(
    notification_id: int,
    response: Response,
    user: dict = Depends(get_current_user),
):
    """确认一条站内通知（置为已读）。"""

    response.headers["Cache-Control"] = "no-store"

    try:
        return mark_notification_read(
            user_id=user["id"],
            notification_id=notification_id,
        )

    except NotificationNotFound:
        raise _not_found() from None

    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail=str(error),
        ) from None
