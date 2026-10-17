"""S4c 发言编排的集成测试：@ 解析 + 定向发言 + 消息落库。"""

import unittest

from tempfile import TemporaryDirectory

from tests.helper import isolate_paths
from src.core.bot_store import BotStore
from src.core.room_store import RoomStore
from src.core.room_service import (
    ask_bots,
    parse_mentions,
    run_room_turn,
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


if __name__ == "__main__":
    unittest.main()
