"""S5b 展开生成（approve_and_expand）的集成测试。"""

import unittest

from tempfile import TemporaryDirectory

from tests.helper import isolate_paths
from src.core.bot_store import BotStore
from src.core.room_store import RoomStore
from src.core.room_service import approve_and_expand, run_room_turn


class TestApproveAndExpand(unittest.TestCase):

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
        self.bot = self.bots.create_bot(
            owner_id="alice",
            name="保守派",
            persona="中立",
            system_prompt="就事论事",
            api_source="platform",
        )
        self.rooms.add_bot_to_room(
            room_id=self.room["id"],
            bot_id=self.bot["id"],
            owner_id="alice",
        )

    def _speak(self, answer):
        def speak(bot, topic, **kw):
            return answer

        return speak

    def test_approve_generates_expand_message(self):
        turn = run_room_turn(
            room_store=self.rooms,
            room_id=self.room["id"],
            owner_id="alice",
            message="怎么看？",
            speak=self._speak("核心观点"),
        )

        hand = self.rooms.raise_hand(
            room_id=self.room["id"],
            turn_id=turn["turn_id"],
            bot_id=self.bot["id"],
            kind="expand",
            reason="想展开",
            owner_id="alice",
        )

        result = approve_and_expand(
            room_store=self.rooms,
            hand_id=hand["id"],
            owner_id="alice",
            speak=self._speak("展开内容"),
        )

        self.assertTrue(result["approved"])
        self.assertEqual(result["content"], "展开内容")

        messages = self.rooms.list_messages(
            room_id=self.room["id"], owner_id="alice"
        )
        roles = [message["role"] for message in messages]
        self.assertIn("expand", roles)

    def test_approve_invalid_hand_returns_false(self):
        result = approve_and_expand(
            room_store=self.rooms,
            hand_id=999,
            owner_id="alice",
        )

        self.assertFalse(result["approved"])


if __name__ == "__main__":
    unittest.main()
