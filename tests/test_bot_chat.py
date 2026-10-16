"""S3a 单人聊天的测试：Bot 身份卡接入 system prompt + Bot 可见性。"""

import unittest

from datetime import datetime, timezone
from tempfile import TemporaryDirectory

from tests.helper import isolate_paths
from src.agent import build_system_prompt
from src.core.bot_store import BotStore


def _now():
    return datetime.now(timezone.utc).astimezone()


class TestBuildSystemPrompt(unittest.TestCase):

    def test_default_prompt(self):
        prompt = build_system_prompt(_now())

        self.assertIn("你是 Personal Agent", prompt)
        self.assertIn("create_reminder", prompt)

    def test_bot_prompt_uses_identity_card(self):
        bot = {
            "name": "保守派",
            "persona": "风险厌恶者",
            "system_prompt": "永远先算代价",
        }

        prompt = build_system_prompt(_now(), bot=bot)

        self.assertIn("保守派", prompt)
        self.assertIn("风险厌恶者", prompt)
        self.assertIn("永远先算代价", prompt)
        # 不再是默认人设
        self.assertNotIn("你是 Personal Agent", prompt)

    def test_bot_prompt_keeps_tool_rules(self):
        bot = {
            "name": "助手",
            "persona": "中立",
            "system_prompt": "就事论事",
        }

        prompt = build_system_prompt(_now(), bot=bot)

        self.assertIn("create_reminder", prompt)
        self.assertIn("list_reminders", prompt)


class TestVisibleBot(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        isolate_paths(self, self.temp)

        self.store = BotStore(self.temp.name + "/bots.db")

    def test_own_bot_is_visible(self):
        bot = self.store.create_bot(
            owner_id="alice",
            name="我的",
            persona="中立",
            system_prompt="就事论事",
            api_source="platform",
        )

        visible = self.store.get_visible_bot(
            bot_id=bot["id"], owner_id="alice"
        )

        self.assertIsNotNone(visible)

    def test_platform_bot_visible_to_all(self):
        bot = self.store.seed_platform_bot(
            name="站长",
            persona="综合",
            system_prompt="汇总",
        )

        self.assertIsNotNone(
            self.store.get_visible_bot(
                bot_id=bot["id"], owner_id="alice"
            )
        )
        self.assertIsNotNone(
            self.store.get_visible_bot(
                bot_id=bot["id"], owner_id="bob"
            )
        )

    def test_others_bot_not_visible(self):
        bot = self.store.create_bot(
            owner_id="alice",
            name="alice 的",
            persona="中立",
            system_prompt="就事论事",
            api_source="platform",
        )

        self.assertIsNone(
            self.store.get_visible_bot(
                bot_id=bot["id"], owner_id="bob"
            )
        )

    def test_disabled_bot_not_visible(self):
        bot = self.store.create_bot(
            owner_id="alice",
            name="我的",
            persona="中立",
            system_prompt="就事论事",
            api_source="platform",
        )
        self.store.set_enabled(
            owner_id="alice", bot_id=bot["id"], enabled=False
        )

        self.assertIsNone(
            self.store.get_visible_bot(
                bot_id=bot["id"], owner_id="alice"
            )
        )


if __name__ == "__main__":
    unittest.main()
