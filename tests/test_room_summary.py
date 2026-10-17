"""S6 主持人汇总的测试：host_summary + host_summarize_turn。"""

import unittest

from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from tests.helper import isolate_paths
from src import agent
from src.core.bot_store import BotStore
from src.core.room_service import host_summarize_turn, run_room_turn
from src.core.room_store import RoomStore


class TestHostSummary(unittest.TestCase):

    def test_host_summary_parses_json(self):
        fake = MagicMock()
        fake.choices = [MagicMock()]
        fake.choices[0].message.content = (
            '{"consensus":"一致","disagreement":"分歧",'
            '"options":[{"label":"做","risk":"贵"}]}'
        )

        with patch.object(
            agent.client.chat.completions,
            "create",
            return_value=fake,
        ):
            result = agent.host_summary(
                [{"name": "A", "content": "观点"}], "话题"
            )

        self.assertEqual(result["consensus"], "一致")
        self.assertEqual(len(result["options"]), 1)

    def test_host_summary_handles_bad_json(self):
        fake = MagicMock()
        fake.choices = [MagicMock()]
        fake.choices[0].message.content = "不是 JSON"

        with patch.object(
            agent.client.chat.completions,
            "create",
            return_value=fake,
        ):
            result = agent.host_summary(
                [{"name": "A", "content": "观点"}], "话题"
            )

        self.assertEqual(result["options"], [])


class TestHostSummarizeTurn(unittest.TestCase):

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

    def test_summarize_stores_summary_message(self):
        turn = run_room_turn(
            room_store=self.rooms,
            room_id=self.room["id"],
            owner_id="alice",
            message="怎么看？",
            speak=lambda bot, topic, **kw: "核心观点",
        )

        def fake_summarize(messages, topic, **kw):
            return {
                "consensus": "一致",
                "disagreement": "分歧",
                "options": [],
            }

        result = host_summarize_turn(
            room_store=self.rooms,
            room_id=self.room["id"],
            owner_id="alice",
            turn_id=turn["turn_id"],
            summarize=fake_summarize,
        )

        self.assertEqual(result["consensus"], "一致")

        messages = self.rooms.list_messages(
            room_id=self.room["id"], owner_id="alice"
        )
        roles = [message["role"] for message in messages]
        self.assertIn("summary", roles)


if __name__ == "__main__":
    unittest.main()
