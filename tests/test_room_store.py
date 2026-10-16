"""聊天室存储层（S4a）的集成测试：房间、成员、消息 + 归属隔离。"""

import unittest

from tempfile import TemporaryDirectory

from tests.helper import isolate_paths
from src.core.bot_store import BotStore
from src.core.room_store import RoomStore, RoomError


class RoomStoreTestCase(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        isolate_paths(self, self.temp)

        db = self.temp.name + "/bots.db"
        self.bots = BotStore(db)
        self.rooms = RoomStore(db)

    def make_room(self, owner="alice", name="讨论房"):
        return self.rooms.create_room(owner_id=owner, name=name)

    def own_bot(self, owner="alice", name="助手"):
        return self.bots.create_bot(
            owner_id=owner,
            name=name,
            persona="中立",
            system_prompt="就事论事",
            api_source="platform",
        )

    def platform_bot(self, name="站长助手"):
        return self.bots.seed_platform_bot(
            name=name,
            persona="综合",
            system_prompt="汇总观点",
        )


class TestRoom(RoomStoreTestCase):

    def test_create_room(self):
        room = self.make_room()

        self.assertEqual(room["owner_id"], "alice")
        self.assertEqual(room["name"], "讨论房")
        self.assertIsInstance(room["id"], int)

    def test_add_own_bot(self):
        room = self.make_room()
        bot = self.own_bot()

        self.assertTrue(
            self.rooms.add_bot_to_room(
                room_id=room["id"],
                bot_id=bot["id"],
                owner_id="alice",
            )
        )

        members = self.rooms.list_room_bots(
            room_id=room["id"], owner_id="alice"
        )
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["name"], "助手")
        self.assertEqual(members[0]["activity"], 100)

    def test_add_platform_bot(self):
        room = self.make_room()
        bot = self.platform_bot()

        self.assertTrue(
            self.rooms.add_bot_to_room(
                room_id=room["id"],
                bot_id=bot["id"],
                owner_id="alice",
            )
        )

        # 站长 Bot 也能拉进房
        self.assertEqual(
            len(
                self.rooms.list_room_bots(
                    room_id=room["id"], owner_id="alice"
                )
            ),
            1,
        )

    def test_cannot_add_others_bot(self):
        room = self.make_room(owner="alice")
        bob_bot = self.own_bot(owner="bob", name="bob 的")

        with self.assertRaises(RoomError):
            self.rooms.add_bot_to_room(
                room_id=room["id"],
                bot_id=bob_bot["id"],
                owner_id="alice",
            )

    def test_cannot_add_to_others_room(self):
        alice_room = self.make_room(owner="alice")
        bot = self.own_bot(owner="bob")

        with self.assertRaises(RoomError):
            self.rooms.add_bot_to_room(
                room_id=alice_room["id"],
                bot_id=bot["id"],
                owner_id="bob",
            )

    def test_add_bot_is_idempotent(self):
        room = self.make_room()
        bot = self.own_bot()

        self.rooms.add_bot_to_room(
            room_id=room["id"], bot_id=bot["id"], owner_id="alice"
        )
        self.rooms.add_bot_to_room(
            room_id=room["id"], bot_id=bot["id"], owner_id="alice"
        )

        self.assertEqual(
            len(
                self.rooms.list_room_bots(
                    room_id=room["id"], owner_id="alice"
                )
            ),
            1,
        )

    def test_list_rooms_with_bot_count(self):
        room = self.make_room()
        self.rooms.add_bot_to_room(
            room_id=room["id"],
            bot_id=self.own_bot()["id"],
            owner_id="alice",
        )

        rooms = self.rooms.list_rooms("alice")

        self.assertEqual(len(rooms), 1)
        self.assertEqual(rooms[0]["bot_count"], 1)


class TestMessage(RoomStoreTestCase):

    def test_add_and_list_message_by_turn(self):
        room = self.make_room()
        bot = self.own_bot()
        turn = "turn-1"

        self.rooms.add_message(
            room_id=room["id"],
            turn_id=turn,
            speaker="user",
            bot_id=None,
            role="core",
            content="这个问题怎么看？",
            owner_id="alice",
        )
        self.rooms.add_message(
            room_id=room["id"],
            turn_id=turn,
            speaker="bot",
            bot_id=bot["id"],
            role="core",
            content="我认为……",
            owner_id="alice",
        )

        messages = self.rooms.list_messages(
            room_id=room["id"], owner_id="alice", turn_id=turn
        )

        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["speaker"], "user")
        self.assertEqual(messages[1]["speaker"], "bot")

    def test_message_requires_room_ownership(self):
        room = self.make_room(owner="alice")

        with self.assertRaises(RoomError):
            self.rooms.add_message(
                room_id=room["id"],
                turn_id="t",
                speaker="user",
                bot_id=None,
                role="core",
                content="越权",
                owner_id="bob",
            )


if __name__ == "__main__":
    unittest.main()
