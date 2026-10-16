"""聊天室发言编排（S4c）：@ 解析 + 遍历 Bot 各说一句。

对应 `docs/CHATROOM_PLAN.md` 的 S4。

一轮发言（turn）：
- 记一条用户消息（speaker=user, role=core）；
- 消息里有 `@bot名` → 只让被 @ 的 Bot 发言；
- 没有 @ → 房间里所有 Bot 各说一句；
- 每个 Bot 用 `utter` 生成一句核心观点（限字数），记入 messages。
"""

import json
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


def approve_and_expand(
    *,
    room_store: RoomStore,
    hand_id: int,
    owner_id: str,
    word_limit: int | None = None,
    speak=None,
) -> dict:
    """批准举手并生成回应（S5b 展开 / S5c 插话）。

    - `kind=expand`：话题 = 该 turn 的用户消息，默认 300 字；
    - `kind=object`：话题 = 原 Bot 最近的展开内容（追问/质疑它），默认 80 字。
    回应记入 messages（speaker=bot, role=kind）。
    """

    from src.core.bot_store import BotStore

    if speak is None:
        from src import agent

        speak = agent.utter

    hand = room_store.get_hand(hand_id=hand_id, owner_id=owner_id)

    if hand is None:
        return {"approved": False}

    if not room_store.approve_hand(
        hand_id=hand_id, owner_id=owner_id
    ):
        return {"approved": False}

    bot = BotStore(room_store.db_path).get_bot(hand["bot_id"])

    if bot is None:
        return {"approved": True, "content": ""}

    messages = room_store.list_messages(
        room_id=hand["room_id"],
        owner_id=owner_id,
        turn_id=hand["turn_id"],
    )

    if hand["kind"] == "object":
        # 插话针对最近的展开内容。
        expands = [
            message["content"]
            for message in messages
            if message["role"] == "expand"
        ]
        topic = expands[-1] if expands else ""
        limit = word_limit or 80
    else:
        topic = next(
            (
                message["content"]
                for message in messages
                if message["speaker"] == "user"
            ),
            "",
        )
        limit = word_limit or 300

    content = speak(bot, topic, word_limit=limit)

    if content:
        room_store.add_message(
            room_id=hand["room_id"],
            turn_id=hand["turn_id"],
            speaker="bot",
            bot_id=hand["bot_id"],
            role=hand["kind"],
            content=content,
            owner_id=owner_id,
        )

    return {
        "approved": True,
        "bot_id": hand["bot_id"],
        "kind": hand["kind"],
        "content": content,
    }


def host_summarize_turn(
    *,
    room_store: RoomStore,
    room_id: int,
    owner_id: str,
    turn_id: str,
    summarize=None,
) -> dict:
    """主持人汇总一轮讨论（S6）：共识/分歧/决策选项记入 messages。

    主持人独立角色，不参与讨论，只把讨论消息流汇总成结构化决策。
    """

    from src.core.bot_store import BotStore

    if summarize is None:
        from src import agent

        summarize = agent.host_summary

    messages = room_store.list_messages(
        room_id=room_id,
        owner_id=owner_id,
        turn_id=turn_id,
    )

    bot_store = BotStore(room_store.db_path)

    turn_messages = []
    topic = ""

    for message in messages:
        if message["speaker"] == "user":
            topic = message["content"]
            continue

        name = "主持人" if message["speaker"] == "host" else "Bot"

        if message["bot_id"] is not None:
            bot = bot_store.get_bot(message["bot_id"])

            if bot is not None:
                name = bot["name"]

        turn_messages.append(
            {"name": name, "content": message["content"]}
        )

    summary = summarize(turn_messages, topic)

    summary_text = json.dumps(summary, ensure_ascii=False)

    room_store.add_message(
        room_id=room_id,
        turn_id=turn_id,
        speaker="host",
        bot_id=None,
        role="summary",
        content=summary_text,
        owner_id=owner_id,
    )

    return summary
