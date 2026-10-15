"""统一收件箱接口。

- `GET /inbox`：当前用户所有待确认的站内消息（监控通知 + 到点的提醒），
  按时间新到旧；
- `POST /inbox/{kind}/{item_id}/ack`：确认其中一条。

两种来源的确认语义不同（监控置 `read`，提醒置 `sent`），
由 `src/core/inbox.py` 路由；接口层只做鉴权与错误映射。
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response

from src.auth_api import get_current_user
from src.core.inbox import (
    InboxNotFound,
    acknowledge,
    list_inbox,
)


router = APIRouter(tags=["收件箱"])

InboxKind = Literal["monitor", "reminder"]


@router.get("/inbox")
def get_inbox(
    response: Response,
    user: dict = Depends(get_current_user),
):
    """列出当前用户待确认的站内消息。"""

    response.headers["Cache-Control"] = "no-store"

    try:
        items = list_inbox(user["id"])

    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail=str(error),
        ) from None

    return {
        "items": items,
    }


@router.post("/inbox/{kind}/{item_id}/ack")
def acknowledge_item(
    kind: InboxKind,
    item_id: int,
    response: Response,
    user: dict = Depends(get_current_user),
):
    """确认一条站内消息。"""

    response.headers["Cache-Control"] = "no-store"

    try:
        return acknowledge(
            user_id=user["id"],
            kind=kind,
            item_id=item_id,
        )

    except InboxNotFound:
        raise HTTPException(
            status_code=404,
            detail="收件箱条目不存在或无权访问",
        ) from None

    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail=str(error),
        ) from None
