"""Bot 实体（S1）的集成测试：身份卡 + 双来源 + 单用户上限 + 归属隔离。

对应 `docs/CHATROOM_PLAN.md` 的 S1。
"""

import unittest

from tempfile import TemporaryDirectory

from tests.helper import isolate_paths
from src.core.bot_store import BotStore, BotError


class BotStoreTestCase(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        isolate_paths(self, self.temp)

        self.store = BotStore(self.temp.name + "/bots.db")

    def create(self, owner="alice", **kw):
        return self.store.create_bot(
            owner_id=owner,
            name=kw.get("name", "助手"),
            persona=kw.get("persona", "谨慎的风险厌恶者"),
            system_prompt=kw.get(
                "system_prompt", "在 persona 立场下发言"
            ),
            api_source=kw.get("api_source", "platform"),
            avatar=kw.get("avatar", "default"),
        )

    def seed(self, name="站长助手"):
        return self.store.seed_platform_bot(
            name=name,
            persona="中立、务实的综合者",
            system_prompt="汇总多方观点，引导决策",
        )


class TestCreateBot(BotStoreTestCase):

    def test_create_bot(self):
        bot = self.create()

        self.assertEqual(bot["owner_id"], "alice")
        self.assertEqual(bot["name"], "助手")
        self.assertEqual(bot["persona"], "谨慎的风险厌恶者")
        self.assertEqual(bot["api_source"], "platform")
        self.assertTrue(bot["enabled"])
        self.assertIsInstance(bot["id"], int)

    def test_byok_bot(self):
        bot = self.create(api_source="byok")

        self.assertEqual(bot["api_source"], "byok")

    def test_invalid_name_rejected(self):
        with self.assertRaises(BotError):
            self.create(name="   ")

    def test_invalid_api_source_rejected(self):
        with self.assertRaises(BotError):
            self.create(api_source="ghost")

    def test_default_avatar(self):
        self.assertEqual(self.create()["avatar"], "default")

    def test_custom_avatar(self):
        bot = self.create(avatar="deepseek")
        self.assertEqual(bot["avatar"], "deepseek")

    def test_invalid_avatar_rejected(self):
        with self.assertRaises(BotError):
            self.create(avatar="ghost")


class TestBotCap(BotStoreTestCase):

    def test_user_capped_at_three(self):
        for index in range(3):
            self.create(name=f"bot-{index}")

        self.assertEqual(self.store.count_owned("alice"), 3)

        with self.assertRaises(BotError):
            self.create(name="bot-4")

        # 上限不因失败而变
        self.assertEqual(self.store.count_owned("alice"), 3)

    def test_platform_bot_not_counted_in_cap(self):
        self.seed()

        for index in range(3):
            self.create(name=f"bot-{index}")

        # 站长 bot 不计入上限，仍能建满 3 个
        self.assertEqual(self.store.count_owned("alice"), 3)


class TestIsolation(BotStoreTestCase):

    def test_list_shows_platform_and_own(self):
        self.seed(name="站长助手")
        self.create(name="我的 bot")

        self.assertEqual(len(self.store.list_bots("alice")), 2)

    def test_list_does_not_cross_users(self):
        self.seed(name="站长助手")
        self.create(owner="alice", name="alice 的 bot")
        self.create(owner="bob", name="bob 的 bot")

        alice_names = {b["name"] for b in self.store.list_bots("alice")}
        bob_names = {b["name"] for b in self.store.list_bots("bob")}

        self.assertIn("alice 的 bot", alice_names)
        self.assertNotIn("bob 的 bot", alice_names)
        self.assertIn("bob 的 bot", bob_names)
        self.assertNotIn("alice 的 bot", bob_names)

    def test_platform_bot_visible_to_all(self):
        self.seed(name="站长助手")

        alice_bots = self.store.list_bots("alice")
        bob_bots = self.store.list_bots("bob")

        self.assertEqual(len(alice_bots), 1)
        self.assertEqual(len(bob_bots), 1)
        self.assertEqual(alice_bots[0]["name"], "站长助手")
        self.assertEqual(bob_bots[0]["name"], "站长助手")


class TestUpdate(BotStoreTestCase):

    def test_update_persona(self):
        bot = self.create()

        self.assertTrue(
            self.store.update_bot(
                owner_id="alice",
                bot_id=bot["id"],
                persona="激进的乐观主义者",
            )
        )

        self.assertEqual(
            self.store.get_bot(bot["id"])["persona"],
            "激进的乐观主义者",
        )

    def test_cannot_update_others_bot(self):
        bot = self.create(owner="alice")

        self.assertFalse(
            self.store.update_bot(
                owner_id="bob",
                bot_id=bot["id"],
                name="篡改",
            )
        )

        self.assertEqual(self.store.get_bot(bot["id"])["name"], "助手")

    def test_update_unknown_field_rejected(self):
        bot = self.create()

        with self.assertRaises(BotError):
            self.store.update_bot(
                owner_id="alice",
                bot_id=bot["id"],
                enabled=False,  # enabled 走 set_enabled，不走 update
            )

    def test_disable_bot(self):
        bot = self.create()

        self.assertTrue(
            self.store.set_enabled(
                owner_id="alice",
                bot_id=bot["id"],
                enabled=False,
            )
        )

        self.assertFalse(self.store.get_bot(bot["id"])["enabled"])

    def test_cannot_disable_others_bot(self):
        bot = self.create(owner="alice")

        self.assertFalse(
            self.store.set_enabled(
                owner_id="bob",
                bot_id=bot["id"],
                enabled=False,
            )
        )

        self.assertTrue(self.store.get_bot(bot["id"])["enabled"])


class TestDefaultBot(BotStoreTestCase):

    def test_get_default_falls_back_to_platform(self):
        platform = self.seed(name="站长助手")
        self.create(name="我的 bot")

        default = self.store.get_default_bot("alice")

        # 未设置时回退到站长 Bot
        self.assertEqual(default["id"], platform["id"])

    def test_set_and_get_default(self):
        self.seed(name="站长助手")
        bot = self.create(name="我的 bot")

        self.store.set_default_bot(user_id="alice", bot_id=bot["id"])

        default = self.store.get_default_bot("alice")
        self.assertEqual(default["id"], bot["id"])

    def test_cannot_set_others_bot_as_default(self):
        self.seed(name="站长助手")
        bob_bot = self.create(owner="bob", name="bob 的")

        with self.assertRaises(BotError):
            self.store.set_default_bot(
                user_id="alice", bot_id=bob_bot["id"]
            )

    def test_no_platform_bot_returns_none(self):
        self.create()  # 只有自己的 bot，无站长 bot，也没设 default

        self.assertIsNone(self.store.get_default_bot("alice"))


if __name__ == "__main__":
    unittest.main()
