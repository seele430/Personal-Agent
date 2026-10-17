"""Bot 管理接口（S2）。

- 创建 / 列出 / 改身份卡 / 禁用，只操作当前登录用户自己的 Bot；
- `user_id` 一律取自服务端会话，不接受前端传入；
- 站长 Bot（owner_id=NULL）对所有人只读可见，是 @ 列表的数据源。
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.auth_api import get_current_user
from src.core.bot_store import BotError, BotStore


router = APIRouter(tags=["Bot"])


def _open_store() -> BotStore:
    return BotStore()


def _not_found() -> HTTPException:
    # 不属于自己和不存在返回同一个响应，
    # 避免别人用 ID 探测他人 Bot 是否存在。
    return HTTPException(
        status_code=404,
        detail="Bot 不存在或无权访问",
    )


class BotCreate(BaseModel):
    name: str
    persona: str
    system_prompt: str
    api_source: str = "platform"
    avatar: str = "default"
    voice: str = "female-1"
    expertise: str | None = None
    style: str | None = None


class BotUpdate(BaseModel):
    name: str | None = None
    persona: str | None = None
    system_prompt: str | None = None
    api_source: str | None = None
    avatar: str | None = None
    voice: str | None = None
    expertise: str | None = None
    style: str | None = None


@router.get("/bots")
def list_bots(user: dict = Depends(get_current_user)):
    """列出当前用户可见的 Bot（站长 Bot + 自己的）。@ 列表数据源。"""

    return {
        "bots": _open_store().list_bots(user["id"]),
    }


@router.post("/bots")
def create_bot(
    payload: BotCreate,
    user: dict = Depends(get_current_user),
):
    """创建自己的 Bot。"""

    try:
        return _open_store().create_bot(
            owner_id=user["id"],
            name=payload.name,
            persona=payload.persona,
            system_prompt=payload.system_prompt,
            api_source=payload.api_source,
            avatar=payload.avatar,
            voice=payload.voice,
            expertise=payload.expertise,
            style=payload.style,
        )

    except BotError as error:
        raise HTTPException(
            status_code=422, detail=str(error)
        ) from None


@router.patch("/bots/{bot_id}")
def update_bot(
    bot_id: int,
    payload: BotUpdate,
    user: dict = Depends(get_current_user),
):
    """更新身份卡字段。"""

    fields = {
        key: value
        for key, value in payload.model_dump().items()
        if value is not None
    }

    if not fields:
        raise HTTPException(
            status_code=422, detail="没有可更新的字段"
        )

    try:
        ok = _open_store().update_bot(
            owner_id=user["id"],
            bot_id=bot_id,
            **fields,
        )

    except BotError as error:
        raise HTTPException(
            status_code=422, detail=str(error)
        ) from None

    if not ok:
        raise _not_found() from None

    return {"updated": True}


@router.post("/bots/{bot_id}/disable")
def disable_bot(
    bot_id: int,
    user: dict = Depends(get_current_user),
):
    try:
        ok = _open_store().set_enabled(
            owner_id=user["id"],
            bot_id=bot_id,
            enabled=False,
        )

    except BotError as error:
        raise HTTPException(
            status_code=422, detail=str(error)
        ) from None

    if not ok:
        raise _not_found() from None

    return {"disabled": True}


@router.post("/bots/{bot_id}/enable")
def enable_bot(
    bot_id: int,
    user: dict = Depends(get_current_user),
):
    try:
        ok = _open_store().set_enabled(
            owner_id=user["id"],
            bot_id=bot_id,
            enabled=True,
        )

    except BotError as error:
        raise HTTPException(
            status_code=422, detail=str(error)
        ) from None

    if not ok:
        raise _not_found() from None

    return {"enabled": True}


@router.delete("/bots/{bot_id}")
def delete_bot(
    bot_id: int,
    user: dict = Depends(get_current_user),
):
    """删除自己的 Bot（平台预置 Bot 删不了）。"""

    try:
        ok = _open_store().delete_bot(
            owner_id=user["id"], bot_id=bot_id
        )

    except BotError as error:
        raise HTTPException(
            status_code=422, detail=str(error)
        ) from None

    if not ok:
        raise _not_found() from None

    return {"deleted": True}


@router.get("/bots/default")
def get_default_bot(
    user: dict = Depends(get_current_user),
):
    """当前用户的默认 Bot（未设置时回退站长 Bot）。"""

    return {
        "bot": _open_store().get_default_bot(user["id"]),
    }


@router.post("/bots/{bot_id}/default")
def set_default_bot(
    bot_id: int,
    user: dict = Depends(get_current_user),
):
    """把某个 Bot 设为当前用户的默认 Bot。"""

    try:
        _open_store().set_default_bot(
            user_id=user["id"],
            bot_id=bot_id,
        )

    except BotError as error:
        raise HTTPException(
            status_code=422, detail=str(error)
        ) from None

    return {"default": True}
