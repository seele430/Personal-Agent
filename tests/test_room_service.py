"""S4c 发言编排的集成测试：@ 解析 + 定向发言 + 消息落库。"""

import unittest

from tempfile import TemporaryDirectory

from tests.helper import isolate_paths
from src.core.bot_store import BotStore
from src.core.room_store import RoomStore
from src.core.room_service import (
    ask_bots,
    bot_join_opening,
    parse_mentions,
    run_opening,
    run_room_turn,
    run_stage,
)


class TestParseMentions(unittest.TestCase):

    def test_mentions_matched_by_name(self):
        bots = [
            {"id": 1, "name": "保守派"},
            {"id": 2, "name": "激进派"},
        ]

        mentioned = parse_mentions("@保守派 怎么看？", bots)

        self.assertEqual([b["id"] for b in mentioned], [1])

    def test_no_mention_returns_empty(self):
        bots = [{"id": 1, "name": "保守派"}]

        self.assertEqual(parse_mentions("怎么看？", bots), [])


class TestRunRoomTurn(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        isolate_paths(self, self.temp)

        db = self.temp.name + "/bots.db"
        self.bots = BotStore(db)
        self.rooms = RoomStore(db)

        self.room = self.rooms.create_room(
            owner_id="alice", name="讨论房"
        )

    def _speak(self, answers):
        def speak(bot, topic, **kw):
            return answers.get(bot["name"], "")

        return speak

    def add_bot(self, name):
        bot = self.bots.create_bot(
            owner_id="alice",
            name=name,
            persona="中立",
            system_prompt="就事论事",
            api_source="platform",
        )
        self.rooms.add_bot_to_room(
            room_id=self.room["id"],
            bot_id=bot["id"],
            owner_id="alice",
        )
        return bot

    def test_all_bots_speak_without_mention(self):
        self.add_bot("保守派")
        self.add_bot("激进派")

        speak = self._speak(
            {"保守派": "先算代价", "激进派": "先冲再说"}
        )

        result = run_room_turn(
            room_store=self.rooms,
            room_id=self.room["id"],
            owner_id="alice",
            message="要不要上？",
            speak=speak,
        )

        self.assertEqual(len(result["replies"]), 2)

        messages = self.rooms.list_messages(
            room_id=self.room["id"], owner_id="alice"
        )
        # 1 用户 + 2 bot
        self.assertEqual(len(messages), 3)

    def test_only_mentioned_bot_speaks(self):
        self.add_bot("保守派")
        self.add_bot("激进派")

        speak = self._speak(
            {"保守派": "先算代价", "激进派": "先冲再说"}
        )

        result = run_room_turn(
            room_store=self.rooms,
            room_id=self.room["id"],
            owner_id="alice",
            message="@保守派 怎么看？",
            speak=speak,
        )

        self.assertEqual(len(result["replies"]), 1)
        self.assertEqual(result["replies"][0]["name"], "保守派")

    def test_user_message_recorded_first(self):
        self.add_bot("保守派")

        speak = self._speak({"保守派": "先算代价"})

        run_room_turn(
            room_store=self.rooms,
            room_id=self.room["id"],
            owner_id="alice",
            message="看看这个",
            speak=speak,
        )

        messages = self.rooms.list_messages(
            room_id=self.room["id"], owner_id="alice"
        )

        self.assertEqual(messages[0]["speaker"], "user")
        self.assertEqual(messages[0]["content"], "看看这个")


    def test_speak_receives_identity_card(self):
        """发言必须拿到完整身份卡：漏了 system_prompt 会让 utter 抛 KeyError。"""

        self.add_bot("保守派")

        received = {}

        def speak(bot, topic, **kw):
            received.update(bot)
            return "先算代价"

        run_room_turn(
            room_store=self.rooms,
            room_id=self.room["id"],
            owner_id="alice",
            message="要不要上？",
            speak=speak,
        )

        self.assertIn("system_prompt", received)
        self.assertIn("persona", received)


    def test_ask_bots_uses_room_name_as_topic(self):
        """「问问 Bot 怎么想」：话题取房间议题，且不记用户消息。"""

        self.add_bot("保守派")

        seen = {}

        def speak(bot, topic, **kw):
            seen["topic"] = topic
            seen["subject"] = kw.get("subject")
            return "我的看法"

        result = ask_bots(
            room_store=self.rooms,
            room_id=self.room["id"],
            owner_id="alice",
            speak=speak,
        )

        self.assertEqual(seen["topic"], self.room["name"])
        self.assertEqual(seen["subject"], self.room["name"])
        self.assertTrue(result["replies"])

        messages = self.rooms.list_messages(
            room_id=self.room["id"], owner_id="alice"
        )

        # 只有 Bot 发言，没有用户消息
        self.assertEqual(
            [m["speaker"] for m in messages], ["bot"]
        )


    def test_host_stays_silent(self):
        """主持人是裁判，默认不表态；只有被 @ 到时才说话。"""

        host = self.bots.create_bot(
            owner_id="alice",
            name="主持",
            persona="中立",
            system_prompt="只汇总",
            api_source="platform",
        )

        room = self.rooms.create_room(
            owner_id="alice",
            name="辩题",
            host_bot_id=host["id"],
        )

        debater = self.bots.create_bot(
            owner_id="alice",
            name="辩手",
            persona="激进",
            system_prompt="就事论事",
            api_source="platform",
        )

        self.rooms.add_bot_to_room(
            room_id=room["id"],
            bot_id=debater["id"],
            owner_id="alice",
        )

        asked = []

        def speak(bot, topic, **kw):
            asked.append(bot["name"])
            return "观点"

        run_room_turn(
            room_store=self.rooms,
            room_id=room["id"],
            owner_id="alice",
            message="大家怎么看",
            speak=speak,
        )

        self.assertEqual(asked, ["辩手"])

        # @ 主持人时才让它开口
        asked.clear()

        run_room_turn(
            room_store=self.rooms,
            room_id=room["id"],
            owner_id="alice",
            message="@主持 你说说",
            speak=speak,
        )

        self.assertEqual(asked, ["主持"])


    def test_opening_host_speaks_first(self):
        """开场：主持人先说规矩，辩手再依次立论。"""

        host = self.bots.create_bot(
            owner_id="alice",
            name="主持",
            persona="中立",
            system_prompt="只汇总",
            api_source="platform",
        )

        room = self.rooms.create_room(
            owner_id="alice",
            name="辩题",
            stances=["支持", "反对"],
            host_bot_id=host["id"],
        )

        debater = self.bots.create_bot(
            owner_id="alice",
            name="正方",
            persona="激进",
            system_prompt="就事论事",
            api_source="platform",
        )

        self.rooms.add_bot_to_room(
            room_id=room["id"],
            bot_id=debater["id"],
            owner_id="alice",
            stance="支持",
        )

        order = []

        def opening(bot, subject, stances, **kw):
            order.append(("host", bot["name"]))
            return "本场辩题是……"

        def debate(bot, subject, stances, **kw):
            order.append(("bot", bot["name"]))
            return "我方主张……"

        result = run_opening(
            room_store=self.rooms,
            room_id=room["id"],
            owner_id="alice",
            opening=opening,
            debate=debate,
        )

        # 主持人必须排在最前
        self.assertEqual(order[0][0], "host")
        self.assertEqual(result["count"], 2)

        messages = self.rooms.list_messages(
            room_id=room["id"], owner_id="alice"
        )

        self.assertEqual(messages[0]["speaker"], "host")
        self.assertEqual(messages[1]["speaker"], "bot")

    def test_opening_skips_bots_without_stance(self):
        """没站队的 Bot 不产出立论（它没有立场可立）。"""

        host = self.bots.create_bot(
            owner_id="alice",
            name="主持",
            persona="中立",
            system_prompt="只汇总",
            api_source="platform",
        )

        room = self.rooms.create_room(
            owner_id="alice",
            name="辩题",
            stances=["支持", "反对"],
            host_bot_id=host["id"],
        )

        idle = self.bots.create_bot(
            owner_id="alice",
            name="旁观",
            persona="中立",
            system_prompt="就事论事",
            api_source="platform",
        )

        self.rooms.add_bot_to_room(
            room_id=room["id"],
            bot_id=idle["id"],
            owner_id="alice",
        )

        asked = []

        result = run_opening(
            room_store=self.rooms,
            room_id=room["id"],
            owner_id="alice",
            opening=lambda bot, subject, stances, **kw: "开场",
            debate=lambda bot, subject, stances, **kw: (
                asked.append(bot["name"]) or "立论"
            ),
        )

        self.assertEqual(asked, [])
        self.assertEqual(result["count"], 1)


    def test_bot_join_opening(self):
        """新辩手进房：定了立场就立刻立论（存进消息流）。"""

        room = self.rooms.create_room(
            owner_id="alice",
            name="辩题",
            stances=["支持", "反对"],
        )

        bot = self.bots.create_bot(
            owner_id="alice",
            name="正方",
            persona="激进",
            system_prompt="就事论事",
            api_source="platform",
        )

        self.rooms.add_bot_to_room(
            room_id=room["id"],
            bot_id=bot["id"],
            owner_id="alice",
            stance="支持",
        )

        content = bot_join_opening(
            room_store=self.rooms,
            room_id=room["id"],
            bot_id=bot["id"],
            owner_id="alice",
            debate=lambda b, s, st, **kw: "我方主张……",
        )

        self.assertEqual(content, "我方主张……")

        messages = self.rooms.list_messages(
            room_id=room["id"], owner_id="alice"
        )

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["speaker"], "bot")
        self.assertEqual(messages[0]["bot_id"], bot["id"])

    def test_join_opening_skips_bot_without_stance(self):
        """没立场的（比如主持人）进房不该产出立论。"""

        room = self.rooms.create_room(
            owner_id="alice", name="辩题"
        )

        bot = self.bots.create_bot(
            owner_id="alice",
            name="主持",
            persona="中立",
            system_prompt="只汇总",
            api_source="platform",
        )

        self.rooms.add_bot_to_room(
            room_id=room["id"],
            bot_id=bot["id"],
            owner_id="alice",
        )

        called = []

        content = bot_join_opening(
            room_store=self.rooms,
            room_id=room["id"],
            bot_id=bot["id"],
            owner_id="alice",
            debate=lambda b, s, st, **kw: (
                called.append(b["name"]) or "立论"
            ),
        )

        self.assertEqual(content, "")
        self.assertEqual(called, [])


    def test_run_stage_rebuttal(self):
        """驳论阶段：每个有立场的辩手产出一条，消息带 stage。"""

        room = self.rooms.create_room(
            owner_id="alice",
            name="辩题",
            stances=["支持", "反对"],
        )

        bot = self.bots.create_bot(
            owner_id="alice",
            name="正方",
            persona="激进",
            system_prompt="就事论事",
            api_source="platform",
        )

        self.rooms.add_bot_to_room(
            room_id=room["id"],
            bot_id=bot["id"],
            owner_id="alice",
            stance="支持",
        )

        result = run_stage(
            room_store=self.rooms,
            room_id=room["id"],
            owner_id="alice",
            stage="rebuttal",
            generators={
                "rebuttal": lambda b, s, st, **kw: "对方说「善是种子」，但……",
            },
        )

        self.assertEqual(result["stage"], "rebuttal")
        self.assertEqual(result["count"], 1)

        messages = self.rooms.list_messages(
            room_id=room["id"], owner_id="alice"
        )

        self.assertEqual(messages[0]["stage"], "rebuttal")

        room_now = self.rooms.get_room(
            room_id=room["id"], owner_id="alice"
        )

        self.assertEqual(room_now["stage"], "rebuttal")


if __name__ == "__main__":
    unittest.main()
