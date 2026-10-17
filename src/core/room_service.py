"""聊天室发言编排（S4c）：@ 解析 + 遍历 Bot 各说一句。

对应 `docs/CHATROOM_PLAN.md` 的 S4。

一轮发言（turn）：
- 记一条用户消息（speaker=user, role=core）；
- 消息里有 `@bot名` → 只让被 @ 的 Bot 发言；
- 没有 @ → 房间里所有 Bot 各说一句；
- 每个 Bot 用 `utter` 生成一句核心观点（限字数），记入 messages。
"""

import json
import logging
import uuid

from src.core.room_store import RoomStore


logger = logging.getLogger("personal_agent.room")


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

    # 房间名 = 议题：喂给 Bot 当上下文，避免它泛泛讲人设。
    room = room_store.get_room(room_id=room_id, owner_id=owner_id)
    subject = room["name"] if room else None
    host_bot_id = room.get("host_bot_id") if room else None

    bots = room_store.list_room_bots(
        room_id=room_id,
        owner_id=owner_id,
    )

    # 用户消息继承当前阶段：否则前端会把它当成「新阶段」画分割线
    room_store.add_message(
        room_id=room_id,
        turn_id=turn_id,
        speaker="user",
        bot_id=None,
        role="core",
        stage=room.get("stage") if room else None,
        content=message,
        owner_id=owner_id,
    )

    # 主持人是裁判、不表态；只有被 @ 到时才让它说话。
    mentioned = parse_mentions(message, bots)

    if mentioned:
        speakers = mentioned
    else:
        speakers = [
            bot for bot in bots if bot["id"] != host_bot_id
        ]

    replies = []

    for bot in speakers:

        try:
            content = speak(
                bot,
                message,
                word_limit=word_limit,
                subject=subject,
            )

        except Exception:
            # 不静默：发言失败要留痕，否则用户只会看到"没反应"。
            logger.exception(
                "Bot 发言失败 room=%s bot=%s",
                room_id,
                bot.get("id"),
            )
            content = ""

        if not content:
            continue

        room_store.add_message(
            room_id=room_id,
            turn_id=turn_id,
            speaker="bot",
            bot_id=bot["id"],
            role="core",
            stage=room.get("stage") if room else None,
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


def ask_bots(
    *,
    room_store: RoomStore,
    room_id: int,
    owner_id: str,
    bot_id: int | None = None,
    word_limit: int = 80,
    speak=None,
) -> dict:
    """「问问 Bot 怎么想」：把**房间议题**抛给 Bot，让它们主动表态。

    传 `bot_id` 则只问这一个（弹框里要单独看它的想法）；
    不传则问房里所有 Bot。

    与 run_room_turn 的区别：用户没说话，所以**不记用户消息**，
    话题直接取房间名（议题）。
    """

    if speak is None:
        from src import agent

        speak = agent.utter

    room = room_store.get_room(room_id=room_id, owner_id=owner_id)

    if room is None:
        raise ValueError("房间不存在或无权访问")

    subject = room["name"]
    turn_id = uuid.uuid4().hex

    bots = room_store.list_room_bots(
        room_id=room_id,
        owner_id=owner_id,
    )

    # 主持人只裁判、不表态（除非专门点它）
    if bot_id is None and room.get("host_bot_id") is not None:
        bots = [
            item
            for item in bots
            if item["id"] != room["host_bot_id"]
        ]

    if bot_id is not None:
        bots = [item for item in bots if item["id"] == bot_id]

    replies = []

    for bot in bots:

        try:
            content = speak(
                bot,
                subject,
                word_limit=word_limit,
                subject=subject,
            )

        except Exception:
            logger.exception(
                "Bot 发言失败 room=%s bot=%s",
                room_id,
                bot.get("id"),
            )
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


def run_opening(
    *,
    room_store: RoomStore,
    room_id: int,
    owner_id: str,
    opening=None,
    debate=None,
    word_limit: int = 400,
) -> dict:
    """开场（S7）：主持人先立规矩，然后各辩手依次开篇立论。

    - `opening`：主持人开场词（默认 `agent.host_opening`）；
    - `debate`：辩手立论（默认 `agent.debater_opening`）。
    """

    if opening is None or debate is None:
        from src import agent

        opening = opening or agent.host_opening
        debate = debate or agent.debater_opening

    room = room_store.get_room(room_id=room_id, owner_id=owner_id)

    if room is None:
        raise ValueError("房间不存在或无权访问")

    subject = room["name"]
    stances = room.get("stances") or []
    host_bot_id = room.get("host_bot_id")

    turn_id = uuid.uuid4().hex
    produced = []

    # 1) 主持人开场（它先说，把议题和流程摆清楚）
    if host_bot_id is not None:
        from src.core.bot_store import BotStore

        host_bot = BotStore(room_store.db_path).get_bot(host_bot_id)

        if host_bot is not None:
            try:
                content = opening(host_bot, subject, stances)

            except Exception:
                logger.exception(
                    "主持人开场失败 room=%s", room_id
                )
                content = ""

            if content:
                room_store.add_message(
                    room_id=room_id,
                    turn_id=turn_id,
                    speaker="host",
                    bot_id=host_bot_id,
                    role="core",
                    stage="opening",
                    content=content,
                    owner_id=owner_id,
                )
                produced.append(content)

    # 2) 各辩手依次开篇立论（主持人除外）
    bots = room_store.list_room_bots(
        room_id=room_id,
        owner_id=owner_id,
    )

    for bot in bots:

        if bot["id"] == host_bot_id or not bot.get("stance"):
            continue

        try:
            content = debate(
                bot, subject, stances, word_limit=word_limit
            )

        except Exception:
            logger.exception(
                "立论失败 room=%s bot=%s",
                room_id,
                bot.get("id"),
            )
            content = ""

        if not content:
            continue

        room_store.add_message(
            room_id=room_id,
            turn_id=turn_id,
            speaker="bot",
            bot_id=bot["id"],
            role="core",
            stage="statement",
            content=content,
            owner_id=owner_id,
        )
        produced.append(content)

    # 开场 + 立论都完了：把流程推进到「立论」
    if produced:
        room_store.advance_stage(
            room_id=room_id,
            owner_id=owner_id,
            stage="statement",
        )

    return {"turn_id": turn_id, "count": len(produced)}


def bot_join_opening(
    *,
    room_store: RoomStore,
    room_id: int,
    bot_id: int,
    owner_id: str,
    debate=None,
    word_limit: int = 400,
) -> str:
    """新辩手进房：立刻做一段开篇立论（定了立场就能发言）。

    没有立场的 Bot（比如主持人）不会产出内容。
    返回立论文本（已存入消息流）；没产出则返回空串。
    """

    if debate is None:
        from src import agent

        debate = agent.debater_opening

    room = room_store.get_room(room_id=room_id, owner_id=owner_id)

    if room is None:
        raise ValueError("房间不存在或无权访问")

    members = room_store.list_room_bots(
        room_id=room_id,
        owner_id=owner_id,
    )

    bot = next(
        (item for item in members if item["id"] == bot_id),
        None,
    )

    if bot is None or not bot.get("stance"):
        return ""

    try:
        content = debate(
            bot,
            room["name"],
            room.get("stances") or [],
            word_limit=word_limit,
        )

    except Exception:
        logger.exception(
            "进房立论失败 room=%s bot=%s", room_id, bot_id
        )
        return ""

    if not content:
        return ""

    room_store.add_message(
        room_id=room_id,
        turn_id=uuid.uuid4().hex,
        speaker="bot",
        bot_id=bot_id,
        role="core",
        stage="statement",
        content=content,
        owner_id=owner_id,
    )

    # 有人立论了，流程至少走到「立论」
    room_store.advance_stage(
        room_id=room_id,
        owner_id=owner_id,
        stage="statement",
    )

    return content


def run_stage(
    *,
    room_store: RoomStore,
    room_id: int,
    owner_id: str,
    stage: str,
    word_limit: int = 350,
    free_rounds: int = 3,
    generators=None,
) -> dict:
    """推进到某个阶段，并生成该阶段的内容（S8）。

    流程：开场 → 立论 → 驳论 → 质询 → 自由交锋 → 结辩 → 复盘。

    - `statement` 由 `run_opening` / `bot_join_opening` 负责立论，这里跳过；
    - `review` 走 `host_summarize_turn`，这里也跳过；
    - 其余阶段：每个有立场的辩手产出一条，消息带上 stage 便于前端分线。
    """

    from src.core.room_store import STAGES

    if stage not in STAGES:
        raise ValueError("未知阶段")

    if generators is None:
        from src import agent

        generators = {
            "rebuttal": agent.debater_rebuttal,
            "crossfire": agent.debater_question,
            "closing": agent.debater_closing,
            "free": agent.utter,
            "vote": agent.host_vote,
        }

    room = room_store.get_room(
        room_id=room_id, owner_id=owner_id
    )

    if room is None:
        raise ValueError("房间不存在或无权访问")

    # 先推进阶段：这样下面写入的消息才带得上 stage
    room_store.advance_stage(
        room_id=room_id, owner_id=owner_id, stage=stage
    )

    subject = room["name"]
    stances = room.get("stances") or []
    host_bot_id = room.get("host_bot_id")

    members = room_store.list_room_bots(
        room_id=room_id, owner_id=owner_id,
    )

    # 投票阶段：主持人宣布开投，辩手不说话，等评委点按钮
    if stage == "vote":
        turn_id = uuid.uuid4().hex
        produced = []

        if host_bot_id is not None:
            from src.core.bot_store import BotStore

            host_bot = BotStore(room_store.db_path).get_bot(host_bot_id)

            if host_bot is not None:
                try:
                    content = generators["vote"](
                        host_bot, subject, stances
                    )

                except Exception:
                    logger.exception(
                        "投票主持词失败 room=%s", room_id
                    )
                    content = ""

                if content:
                    room_store.add_message(
                        room_id=room_id,
                        turn_id=turn_id,
                        speaker="host",
                        bot_id=host_bot_id,
                        role="core",
                        stage="vote",
                        content=content,
                        owner_id=owner_id,
                    )
                    produced.append(content)

        return {
            "stage": stage,
            "turn_id": turn_id,
            "count": len(produced),
        }

    debaters = [
        item
        for item in members
        if item["id"] != host_bot_id and item.get("stance")
    ]

    history = room_store.list_messages(
        room_id=room_id, owner_id=owner_id
    )

    transcript = "\n".join(
        f"「{item['speaker']}」：{item['content']}"
        for item in history
        if item["speaker"] in ("bot", "host")
    )

    turn_id = uuid.uuid4().hex
    produced = []

    # 交锋**回合制**：进入阶段时只让第一位说一条。
    # 后面每一条都由用户点「下一位发言」触发 /turn 生成，
    # 这样才是一问一答的拉锯，而不是一次性喷 6 条。
    rounds = 1

    for _round in range(rounds):

        for bot in debaters:

            # 对手说过的话（驳论 / 质询要用）
            opponent_speech = "\n".join(
                item["content"]
                for item in history
                if item["speaker"] == "bot"
                and item["bot_id"] != bot["id"]
            ) or "（对方尚未发言）"

            try:
                if stage == "rebuttal":
                    content = generators["rebuttal"](
                        bot,
                        subject,
                        stances,
                        opponent_speech=opponent_speech,
                        word_limit=word_limit,
                    )
                elif stage == "crossfire":
                    content = generators["crossfire"](
                        bot,
                        subject,
                        stances,
                        opponent_speech=opponent_speech,
                    )
                elif stage == "closing":
                    content = generators["closing"](
                        bot,
                        subject,
                        stances,
                        history=transcript,
                    )
                elif stage == "free":
                    # 交锋必须接得上对方的话：把对手**最近一句**当成
                    # 要回应的对象，否则两边只会各自复读立场。
                    last_opponent = next(
                        (
                            item["content"]
                            for item in reversed(history)
                            if item["speaker"] == "bot"
                            and item["bot_id"] != bot["id"]
                        ),
                        "",
                    )

                    content = generators["free"](
                        bot,
                        last_opponent or subject,
                        word_limit=120,
                        subject=subject,
                    )
                else:
                    content = ""

            except Exception:
                logger.exception(
                    "阶段发言失败 stage=%s room=%s bot=%s",
                    stage,
                    room_id,
                    bot.get("id"),
                )
                content = ""

            if not content:
                continue

            room_store.add_message(
                room_id=room_id,
                turn_id=turn_id,
                speaker="bot",
                bot_id=bot["id"],
                role="core",
                stage=stage,
                content=content,
                owner_id=owner_id,
            )
            produced.append(content)

            # 让后面的轮次 / 下一个人看得见这一句
            history.append(
                {
                    "speaker": "bot",
                    "bot_id": bot["id"],
                    "content": content,
                }
            )

    return {
        "stage": stage,
        "turn_id": turn_id,
        "count": len(produced),
    }


def run_free_turn(
    *,
    room_store: RoomStore,
    room_id: int,
    owner_id: str,
    word_limit: int = 160,
    generators=None,
) -> dict:
    """交锋的**一个回合**：轮到谁，就让谁说一条。

    轮转顺序按房间成员顺序循环，用「本阶段已有发言条数」定位，
    这样不需要额外存游标。
    """

    if generators is None:
        from src import agent

        generators = {"free_turn": agent.debater_free_turn}

    room = room_store.get_room(
        room_id=room_id, owner_id=owner_id
    )

    if room is None:
        raise ValueError("房间不存在或无权访问")

    if room.get("stage") != "free":
        raise ValueError("当前不在交锋阶段")

    members = room_store.list_room_bots(
        room_id=room_id, owner_id=owner_id,
    )

    host_bot_id = room.get("host_bot_id")

    debaters = [
        item
        for item in members
        if item["id"] != host_bot_id and item.get("stance")
    ]

    if len(debaters) < 2:
        raise ValueError("至少需要两个有立场的 Bot 才能交锋")

    history = room_store.list_messages(
        room_id=room_id, owner_id=owner_id
    )

    stage_msgs = [item for item in history if item.get("stage") == "free"]

    # 轮转按**上一位实际发言的人**算，不按「已有条数 % 人数」。
    # 后者在成员变动时会错位：比如把 Bot-1 换成 Bot-3，计数还停在旧位置，
    # 指针会指向一个不在场上的人（表现就是"某个人永远轮不到"）。
    # `debaters` 本身是按 joined_at（拉进房间的顺序）排的。
    last_id = next(
        (
            item["bot_id"]
            for item in reversed(stage_msgs)
            if item.get("bot_id") is not None
        ),
        None,
    )

    last_index = next(
        (
            index
            for index, item in enumerate(debaters)
            if item["id"] == last_id
        ),
        -1,
    )

    bot = (
        debaters[0]
        if last_index < 0
        else debaters[(last_index + 1) % len(debaters)]
    )

    opponent = next(
        (
            item["content"]
            for item in reversed(history)
            if item["speaker"] == "bot"
            and item["bot_id"] != bot["id"]
        ),
        "",
    )

    try:
        content = generators["free_turn"](
            bot,
            opponent,
            room["name"],
            room.get("stances") or [],
            word_limit=word_limit,
        )

    except Exception:
        logger.exception(
            "交锋回合失败 room=%s bot=%s", room_id, bot.get("id")
        )
        content = ""

    if not content:
        raise ValueError("本轮没有产出内容，再试一次")

    room_store.add_message(
        room_id=room_id,
        turn_id=uuid.uuid4().hex,
        speaker="bot",
        bot_id=bot["id"],
        role="core",
        stage="free",
        content=content,
        owner_id=owner_id,
    )

    return {
        "bot_id": bot["id"],
        "bot_name": bot["name"],
        "content": content,
    }


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
    turn_id: str | None = None,
    summarize=None,
) -> dict:
    """主持人汇总一轮讨论（S6）：共识/分歧/决策选项记入 messages。

    主持人独立角色，不参与讨论，只把讨论消息流汇总成结构化决策。

    `turn_id` 传 None 表示**汇总整场**（复盘用）。按单一 turn 过滤会把
    投票那类"只有用户发言"的 turn 汇总成空记录，直接报错。
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
    room = room_store.get_room(
        room_id=room_id, owner_id=owner_id
    ) or {}

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

    # 主持人 = 房间里指定的那个 Bot（通常是默认助手）
    host_bot = None

    if room.get("host_bot_id") is not None:
        host_bot = bot_store.get_bot(room["host_bot_id"])

    summary = summarize(turn_messages, topic, bot=host_bot)

    summary_text = json.dumps(summary, ensure_ascii=False)

    room_store.add_message(
        room_id=room_id,
        turn_id=turn_id or uuid.uuid4().hex,
        speaker="host",
        bot_id=None,
        role="summary",
        content=summary_text,
        owner_id=owner_id,
    )

    return summary
