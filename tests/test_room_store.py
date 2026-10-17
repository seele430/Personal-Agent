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
        # 发言要读身份卡：漏了 system_prompt 会让 utter 抛 KeyError（曾踩过）
        self.assertEqual(members[0]["system_prompt"], "就事论事")

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


    def test_create_room_with_stances(self):
        """立场：去空、去重；列表接口要能读回来。"""

        room = self.rooms.create_room(
            owner_id="alice",
            name="辩题",
            stances=["支持", "反对", "   ", "支持"],
        )

        self.assertEqual(room["stances"], ["支持", "反对"])

        listed = self.rooms.list_rooms("alice")

        self.assertEqual(listed[0]["stances"], ["支持", "反对"])

    def test_archive_and_delete_room(self):
        """归档=软隐藏（数据还在）；删除=真删。"""

        room = self.rooms.create_room(
            owner_id="alice", name="旧的"
        )

        self.assertTrue(
            self.rooms.archive_room(
                room_id=room["id"], owner_id="alice"
            )
        )
        self.assertEqual(self.rooms.list_rooms("alice"), [])
        self.assertEqual(
            len(
                self.rooms.list_rooms(
                    "alice", include_archived=True
                )
            ),
            1,
        )

        self.assertTrue(
            self.rooms.delete_room(
                room_id=room["id"], owner_id="alice"
            )
        )
        self.assertEqual(
            self.rooms.list_rooms("alice", include_archived=True),
            [],
        )

    def test_archive_room_is_owner_scoped(self):
        """别人的房间归档不动。"""

        room = self.rooms.create_room(
            owner_id="alice", name="alice 的"
        )

        self.assertFalse(
            self.rooms.archive_room(
                room_id=room["id"], owner_id="bob"
            )
        )
        self.assertEqual(len(self.rooms.list_rooms("alice")), 1)


    def test_add_bot_with_stance(self):
        """拉 Bot 进房时记下它的立场（阵营）。"""

        room = self.rooms.create_room(
            owner_id="alice",
            name="辩题",
            stances=["支持", "反对"],
        )
        bot = self.own_bot()

        self.rooms.add_bot_to_room(
            room_id=room["id"],
            bot_id=bot["id"],
            owner_id="alice",
            stance="支持",
        )

        members = self.rooms.list_room_bots(
            room_id=room["id"], owner_id="alice"
        )

        self.assertEqual(members[0]["stance"], "支持")


    def test_create_room_with_host(self):
        """建房间带主持人：它自动进房，且不占立场（是裁判不是辩手）。"""

        bot = self.own_bot()

        room = self.rooms.create_room(
            owner_id="alice",
            name="辩题",
            stances=["支持", "反对"],
            host_bot_id=bot["id"],
        )

        self.assertEqual(room["host_bot_id"], bot["id"])

        members = self.rooms.list_room_bots(
            room_id=room["id"], owner_id="alice"
        )

        self.assertEqual(len(members), 1)
        self.assertIsNone(members[0]["stance"])

    def test_apply_stance_penalty(self):
        """结算：站对边的不扣，其余扣。"""

        room = self.rooms.create_room(
            owner_id="alice",
            name="辩题",
            stances=["支持", "反对"],
        )
        winner = self.own_bot(name="正方")
        loser = self.own_bot(name="反方")

        self.rooms.add_bot_to_room(
            room_id=room["id"],
            bot_id=winner["id"],
            owner_id="alice",
            stance="支持",
        )
        self.rooms.add_bot_to_room(
            room_id=room["id"],
            bot_id=loser["id"],
            owner_id="alice",
            stance="反对",
        )

        penalized = self.rooms.apply_stance_penalty(
            room_id=room["id"],
            owner_id="alice",
            chosen_stance="支持",
            penalty=30,
        )

        self.assertEqual(len(penalized), 1)

        members = {
            item["id"]: item
            for item in self.rooms.list_room_bots(
                room_id=room["id"], owner_id="alice"
            )
        }

        self.assertEqual(members[winner["id"]]["activity"], 100)
        self.assertEqual(members[loser["id"]]["activity"], 70)

    def test_penalty_never_below_zero(self):
        """活跃值扣到 0 就打住，不会变负数。"""

        room = self.rooms.create_room(
            owner_id="alice", name="辩题"
        )
        bot = self.own_bot()

        self.rooms.add_bot_to_room(
            room_id=room["id"],
            bot_id=bot["id"],
            owner_id="alice",
        )

        for _ in range(2):
            self.rooms.apply_stance_penalty(
                room_id=room["id"],
                owner_id="alice",
                chosen_stance=None,
                penalty=100,
            )

        members = self.rooms.list_room_bots(
            room_id=room["id"], owner_id="alice"
        )

        self.assertEqual(members[0]["activity"], 0)


    def test_advance_stage(self):
        """阶段只能往前走。"""

        room = self.make_room()

        self.assertTrue(
            self.rooms.advance_stage(
                room_id=room["id"],
                owner_id="alice",
                stage="rebuttal",
            )
        )
        self.assertEqual(
            self.rooms.get_room(
                room_id=room["id"], owner_id="alice"
            )["stage"],
            "rebuttal",
        )

        # 不能倒退
        self.assertFalse(
            self.rooms.advance_stage(
                room_id=room["id"],
                owner_id="alice",
                stage="statement",
            )
        )

    def test_unknown_stage_rejected(self):
        room = self.make_room()

        with self.assertRaises(RoomError):
            self.rooms.advance_stage(
                room_id=room["id"],
                owner_id="alice",
                stage="乱写",
            )


    def test_submit_and_aggregate_scores(self):
        """打分：房主权重重 1.5，加权平均要算得对。"""

        room = self.make_room()
        bot = self.own_bot()

        self.rooms.submit_score(
            room_id=room["id"],
            bot_id=bot["id"],
            voter_id="alice",
            score=8,
            weight=1.5,
        )

        scores = self.rooms.list_scores(
            room_id=room["id"], owner_id="alice"
        )

        self.assertEqual(len(scores), 1)
        self.assertEqual(scores[0]["weighted"], 8.0)
        self.assertEqual(scores[0]["votes"], 1)

    def test_score_out_of_range_rejected(self):
        room = self.make_room()
        bot = self.own_bot()

        with self.assertRaises(RoomError):
            self.rooms.submit_score(
                room_id=room["id"],
                bot_id=bot["id"],
                voter_id="alice",
                score=11,
            )


    def test_submit_and_tally_votes(self):
        """投票：按立场加权汇总。"""

        room = self.make_room()

        self.rooms.submit_vote(
            room_id=room["id"],
            voter_id="alice",
            stance="支持",
            weight=1.5,
        )

        result = self.rooms.list_votes(
            room_id=room["id"], owner_id="alice"
        )

        self.assertEqual(len(result["tally"]), 1)
        self.assertEqual(result["tally"][0]["stance"], "支持")
        self.assertEqual(result["tally"][0]["weighted"], 1.5)

    def test_vote_overwrites_same_voter(self):
        """同一个人改票要覆盖，不能算两票。"""

        room = self.make_room()

        self.rooms.submit_vote(
            room_id=room["id"], voter_id="alice",
            stance="支持", weight=1.5,
        )
        self.rooms.submit_vote(
            room_id=room["id"], voter_id="alice",
            stance="反对", weight=1.5,
        )

        result = self.rooms.list_votes(
            room_id=room["id"], owner_id="alice"
        )

        self.assertEqual(len(result["tally"]), 1)
        self.assertEqual(result["tally"][0]["stance"], "反对")


if __name__ == "__main__":
    unittest.main()
