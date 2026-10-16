"""聊天室接口（S4c）。

- 创建房间 / 列房间 / 拉 Bot / 列成员 / 发消息触发发言 / 拉消息流；
- `user_id` 一律取自服务端会话；
- 发消息会真实调用模型让各 Bot 发言（走 `room_service.run_room_turn`）。
"""

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.auth_api import get_current_user
from src.core.room_service import (
    approve_and_expand,
    host_summarize_turn,
    run_room_turn,
)
from src.core.room_store import RoomError, RoomStore


router = APIRouter(tags=["聊天室"])


def _open_store() -> RoomStore:
    return RoomStore()


def _not_found() -> HTTPException:
    # 不属于自己和不存在返回同一个响应，避免探测。
    return HTTPException(
        status_code=404,
        detail="房间不存在或无权访问",
    )


class RoomCreate(BaseModel):
    name: str


class BotAdd(BaseModel):
    bot_id: int


class MessageSend(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    word_limit: int = Field(default=80, ge=10, le=200)


@router.post("/rooms")
def create_room(
    payload: RoomCreate,
    user: dict = Depends(get_current_user),
):
    try:
        return _open_store().create_room(
            owner_id=user["id"], name=payload.name
        )

    except RoomError as error:
        raise HTTPException(
            status_code=422, detail=str(error)
        ) from None


@router.get("/rooms")
def list_rooms(user: dict = Depends(get_current_user)):
    return {"rooms": _open_store().list_rooms(user["id"])}


@router.post("/rooms/{room_id}/bots")
def add_bot_to_room(
    room_id: int,
    payload: BotAdd,
    user: dict = Depends(get_current_user),
):
    try:
        _open_store().add_bot_to_room(
            room_id=room_id,
            bot_id=payload.bot_id,
            owner_id=user["id"],
        )

    except RoomError as error:
        raise HTTPException(
            status_code=422, detail=str(error)
        ) from None

    return {"added": True}


@router.get("/rooms/{room_id}/bots")
def list_room_bots(
    room_id: int,
    user: dict = Depends(get_current_user),
):
    try:
        bots = _open_store().list_room_bots(
            room_id=room_id, owner_id=user["id"]
        )

    except RoomError:
        raise _not_found() from None

    return {"bots": bots}


@router.post("/rooms/{room_id}/messages")
def send_message(
    room_id: int,
    payload: MessageSend,
    user: dict = Depends(get_current_user),
):
    store = _open_store()

    try:
        result = run_room_turn(
            room_store=store,
            room_id=room_id,
            owner_id=user["id"],
            message=payload.message,
            word_limit=payload.word_limit,
        )

    except RoomError:
        raise _not_found() from None

    return result


@router.get("/rooms/{room_id}/messages")
def list_messages(
    room_id: int,
    user: dict = Depends(get_current_user),
):
    try:
        messages = _open_store().list_messages(
            room_id=room_id, owner_id=user["id"]
        )

    except RoomError:
        raise _not_found() from None

    return {"messages": messages}


class HandRaise(BaseModel):
    bot_id: int
    turn_id: str
    kind: str = "expand"
    reason: str = Field(min_length=1, max_length=200)


class HandReject(BaseModel):
    unfair: bool = False


class Decision(BaseModel):
    option: str = Field(min_length=1, max_length=200)


@router.post("/rooms/{room_id}/hands")
def raise_hand(
    room_id: int,
    payload: HandRaise,
    user: dict = Depends(get_current_user),
):
    try:
        return _open_store().raise_hand(
            room_id=room_id,
            turn_id=payload.turn_id,
            bot_id=payload.bot_id,
            kind=payload.kind,
            reason=payload.reason,
            owner_id=user["id"],
        )

    except RoomError as error:
        raise HTTPException(
            status_code=422, detail=str(error)
        ) from None


@router.get("/rooms/{room_id}/hands")
def list_hands(
    room_id: int,
    status: str | None = None,
    user: dict = Depends(get_current_user),
):
    try:
        hands = _open_store().list_hands(
            room_id=room_id,
            owner_id=user["id"],
            status=status,
        )

    except RoomError:
        raise _not_found() from None

    return {"hands": hands}


@router.post("/hands/{hand_id}/approve")
def approve_hand(
    hand_id: int,
    user: dict = Depends(get_current_user),
):
    try:
        return approve_and_expand(
            room_store=_open_store(),
            hand_id=hand_id,
            owner_id=user["id"],
        )

    except RoomError:
        raise _not_found() from None


@router.post("/hands/{hand_id}/reject")
def reject_hand(
    hand_id: int,
    payload: HandReject,
    user: dict = Depends(get_current_user),
):
    try:
        return _open_store().reject_hand(
            hand_id=hand_id,
            owner_id=user["id"],
            unfair=payload.unfair,
        )

    except RoomError:
        raise _not_found() from None


@router.post("/rooms/{room_id}/turns/{turn_id}/summary")
def summarize_turn(
    room_id: int,
    turn_id: str,
    user: dict = Depends(get_current_user),
):
    try:
        return host_summarize_turn(
            room_store=_open_store(),
            room_id=room_id,
            owner_id=user["id"],
            turn_id=turn_id,
        )

    except RoomError:
        raise _not_found() from None


@router.post("/rooms/{room_id}/turns/{turn_id}/decision")
def make_decision(
    room_id: int,
    turn_id: str,
    payload: Decision,
    user: dict = Depends(get_current_user),
):
    """用户选择一个决策选项，由默认 Bot 执行工具（S6 收口）。"""

    from src import agent
    from src.core.bot_store import BotStore
    from src.core.reminder_identity import bind_authenticated_reminder

    store = _open_store()

    try:
        messages = store.list_messages(
            room_id=room_id,
            owner_id=user["id"],
            turn_id=turn_id,
        )

    except RoomError:
        raise _not_found() from None

    summary_msg = next(
        (
            message
            for message in reversed(messages)
            if message["role"] == "summary"
        ),
        None,
    )

    if summary_msg is None:
        raise HTTPException(
            status_code=409,
            detail="还没有汇总，请先触发 summary",
        )

    summary = json.loads(summary_msg["content"])
    options = summary.get("options", [])

    chosen = next(
        (
            option
            for option in options
            if option.get("label") == payload.option
        ),
        None,
    )

    if chosen is None:
        raise HTTPException(
            status_code=422,
            detail="选项不存在",
        )

    default_bot = BotStore(store.db_path).get_default_bot(user["id"])

    if default_bot is None:
        raise HTTPException(
            status_code=409,
            detail="没有可用的默认 Bot",
        )

    instruction = (
        f"用户选择了这个决策：{chosen['label']}。"
        f"代价/风险：{chosen.get('risk', '')}。"
        f"请执行这个决策。"
    )

    request_id = uuid.uuid4().hex

    with bind_authenticated_reminder(
        user_id=user["id"],
        request_id=request_id,
        role=user["role"],
    ):
        reply = agent.chat(instruction, bot=default_bot)

    store.add_message(
        room_id=room_id,
        turn_id=turn_id,
        speaker="bot",
        bot_id=default_bot["id"],
        role="decision",
        content=reply,
        owner_id=user["id"],
    )

    return {
        "decision": chosen["label"],
        "reply": reply,
    }
