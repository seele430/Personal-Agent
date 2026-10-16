"""聊天室发言编排（S4c）：@ 解析 + 遍历 Bot 各说一句。

对应 `docs/CHATROOM_PLAN.md` 的 S4。

一轮发言（turn）：
- 记一条用户消息（speaker=user, role=core）；
- 消息里有 `@bot名` → 只让被 @ 的 Bot 发言；
- 没有 @ → 房间里所有 Bot 各说一句；
- 每个 Bot 用 `utter` 生成一句核心观点（限字数），记入 messages。
"""

import uuid

from src.core.room_store import RoomStore


def parse_mentions(message: str, bots: list[dict]) -> list[dict]:
    """解析 `@bot名`，返回被 @ 的 Bot（按名字精确匹配）。"""

    return [
        bot
        for bot in bots
        if f"@{bot['name']}" in message
    ]


def run_room_turn(
    *,
    room_store: RoomStore,
    room_id: int,
    owner_id: str,
    message: str,
    word_limit: int = 80,
    speak=None,
) -> dict:
    """执行一轮发言，返回 `{"turn_id", "replies"}`。

    `speak` 可注入（默认 `agent.utter`），便于测试 mock。
    """

    if speak is None:
        from src import agent

        speak = agent.utter

    turn_id = uuid.uuid4().hex

    bots = room_store.list_room_bots(
        room_id=room_id,
        owner_id=owner_id,
    )

    room_store.add_message(
        room_id=room_id,
        turn_id=turn_id,
        speaker="user",
        bot_id=None,
        role="core",
        content=message,
        owner_id=owner_id,
    )

    mentioned = parse_mentions(message, bots)
    speakers = mentioned if mentioned else bots

    replies = []

    for bot in speakers:

        try:
            content = speak(bot, message, word_limit=word_limit)

        except Exception:
            content = ""

        if not content:
            continue

        room_store.add_message(
            room_id=room_id,
            turn_id=turn_id,
            speaker="bot",
            bot_id=bot["id"],
            role="core",
            content=content,
            owner_id=owner_id,
        )

        replies.append(
            {
                "bot_id": bot["id"],
                "name": bot["name"],
                "content": content,
            }
        )

    return {"turn_id": turn_id, "replies": replies}
