"""S4 发言函数（utter）的单元测试：身份卡注入 + 限字数 + 不调工具。"""

import unittest

from unittest.mock import MagicMock, patch

from src import agent


class TestUtter(unittest.TestCase):

    def _bot(self):
        return {
            "name": "保守派",
            "persona": "风险厌恶者",
            "system_prompt": "永远先算代价",
        }

    def _fake_response(self, text="先别急，代价没算清。"):
        fake = MagicMock()
        fake.choices = [MagicMock()]
        fake.choices[0].message.content = text
        return fake

    def test_utter_calls_model_with_identity_card(self):
        with patch.object(
            agent.client.chat.completions,
            "create",
            return_value=self._fake_response(),
        ) as create:
            reply = agent.utter(
                self._bot(), "要不要上这个功能？", word_limit=80
            )

        self.assertEqual(reply, "先别急，代价没算清。")

        messages = create.call_args.kwargs["messages"]
        system = messages[0]["content"]
        user = messages[1]["content"]

        self.assertIn("保守派", system)
        self.assertIn("风险厌恶者", system)
        self.assertIn("永远先算代价", system)
        self.assertIn("要不要上这个功能", user)
        self.assertIn("80 字", user)

    def test_utter_does_not_call_tools(self):
        with patch.object(
            agent.client.chat.completions,
            "create",
            return_value=self._fake_response(),
        ) as create:
            agent.utter(self._bot(), "话题")

        kwargs = create.call_args.kwargs

        # 纯生成：不传 tools / tool_choice
        self.assertNotIn("tools", kwargs)
        self.assertNotIn("tool_choice", kwargs)

    def test_utter_caps_output_tokens(self):
        with patch.object(
            agent.client.chat.completions,
            "create",
            return_value=self._fake_response(),
        ) as create:
            agent.utter(self._bot(), "话题", word_limit=50)

        kwargs = create.call_args.kwargs
        self.assertLess(kwargs["max_tokens"], 200)

    def test_invalid_bot_rejected(self):
        with self.assertRaises(ValueError):
            agent.utter({}, "话题")

    def test_empty_topic_rejected(self):
        with self.assertRaises(ValueError):
            agent.utter(self._bot(), "   ")


if __name__ == "__main__":
    unittest.main()
