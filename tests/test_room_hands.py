"""S5 举手状态机 + 活跃度的集成测试。"""

import unittest

from tempfile import TemporaryDirectory

from tests.helper import isolate_paths
from src.core.bot_store import BotStore
from src.core.room_store import RoomStore, RoomError


class TestHands(unittest.TestCase):

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

    def raise_hand(self, kind="expand", reason="想展开说明"):
        return self.rooms.raise_hand(
            room_id=self.room["id"],
            turn_id="turn-1",
            bot_id=self.bot["id"],
            kind=kind,
            reason=reason,
            owner_id="alice",
        )

    def test_raise_hand(self):
        hand = self.raise_hand()

        self.assertEqual(hand["status"], "pending")
        self.assertEqual(hand["kind"], "expand")

    def test_raise_hand_requires_membership(self):
        outsider = self.bots.create_bot(
            owner_id="alice",
            name="外人",
            persona="中立",
            system_prompt="就事论事",
            api_source="platform",
        )

        with self.assertRaises(RoomError):
            self.rooms.raise_hand(
                room_id=self.room["id"],
                turn_id="t",
                bot_id=outsider["id"],
                kind="expand",
                reason="插话",
                owner_id="alice",
            )

    def test_approve_hand(self):
        hand = self.raise_hand()

        self.assertTrue(
            self.rooms.approve_hand(
                hand_id=hand["id"], owner_id="alice"
            )
        )

        hands = self.rooms.list_hands(
            room_id=self.room["id"], owner_id="alice"
        )
        self.assertEqual(hands[0]["status"], "approved")

    def test_reject_hand_without_penalty(self):
        hand = self.raise_hand()

        result = self.rooms.reject_hand(
            hand_id=hand["id"], owner_id="alice", unfair=False
        )

        self.assertTrue(result["changed"])
        self.assertFalse(result["unfair"])

        members = self.rooms.list_room_bots(
            room_id=self.room["id"], owner_id="alice"
        )
        self.assertEqual(members[0]["activity"], 100)

    def test_reject_unfair_hand_penalizes_activity(self):
        hand = self.raise_hand(kind="object", reason="无脑反驳")

        result = self.rooms.reject_hand(
            hand_id=hand["id"], owner_id="alice", unfair=True
        )

        self.assertTrue(result["changed"])

        members = self.rooms.list_room_bots(
            room_id=self.room["id"], owner_id="alice"
        )
        self.assertEqual(members[0]["activity"], 80)

    def test_cannot_approve_others_hand(self):
        hand = self.raise_hand()

        self.assertFalse(
            self.rooms.approve_hand(
                hand_id=hand["id"], owner_id="bob"
            )
        )


if __name__ == "__main__":
    unittest.main()
