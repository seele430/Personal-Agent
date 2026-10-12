
"""模型调用长度限制测试：不调用真实 API。"""

import unittest

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from src import agent


class TestAgentBounds(unittest.TestCase):

    def setUp(self):
        self.init_patch = patch.object(
            agent,
            "init_db",
        )
        self.init_patch.start()

    def tearDown(self):
        self.init_patch.stop()

    @staticmethod
    def reply(
        text="模拟回复",
        *,
        finish_reason="stop",
        tool_calls=None,
    ):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=finish_reason,
                    message=SimpleNamespace(
                        content=text,
                        tool_calls=tool_calls,
                    ),
                ),
            ],
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
            ),
        )

    def test_deepseek_uses_bounded_output(self):
        with patch.object(
            agent,
            "_is_deepseek_flash",
            return_value=True,
        ):
            with patch.object(
                agent.client.chat.completions,
                "create",
                return_value=self.reply(),
            ) as model:
                result = agent.chat("你好")

        self.assertEqual(result, "模拟回复")

        kwargs = model.call_args.kwargs

        self.assertEqual(
            kwargs["max_tokens"],
            agent.MAX_OUTPUT_TOKENS,
        )

        self.assertEqual(
            kwargs["extra_body"],
            {"thinking": {"type": "disabled"}},
        )

        model.assert_called_once()

    def test_other_provider_has_no_deepseek_parameter(
        self,
    ):
        with patch.object(
            agent,
            "_is_deepseek_flash",
            return_value=False,
        ):
            with patch.object(
                agent.client.chat.completions,
                "create",
                return_value=self.reply(),
            ) as model:
                agent.chat("你好")

        kwargs = model.call_args.kwargs

        self.assertNotIn(
            "extra_body",
            kwargs,
        )

        self.assertEqual(
            kwargs["max_tokens"],
            agent.MAX_OUTPUT_TOKENS,
        )

    def test_oversized_input_rejected_before_model(
        self,
    ):
        too_long = (
            "a" * (
                agent.MAX_USER_MESSAGE_BYTES + 1
            )
        )

        with patch.object(
            agent.client.chat.completions,
            "create",
        ) as model:
            with self.assertRaises(ValueError):
                agent.chat(too_long)

        model.assert_not_called()

    def test_frontend_character_limit_is_compatible(
        self,
    ):
        # 8000 个四字节 Unicode 字符。
        message = "🧪" * 8000

        with patch.object(
            agent.client.chat.completions,
            "create",
            return_value=self.reply(),
        ):
            result = agent.chat(message)

        self.assertEqual(result, "模拟回复")

    def test_truncated_response_is_not_accepted(
        self,
    ):
        with patch.object(
            agent.client.chat.completions,
            "create",
            return_value=self.reply(
                finish_reason="length",
            ),
        ):
            with self.assertRaises(RuntimeError):
                agent.chat("测试截断")

    def test_reminder_limits_both_model_calls(self):
        tool = SimpleNamespace(
            id="tool_1",
            function=SimpleNamespace(
                name="create_reminder",
                arguments=(
                    '{"message":"喝水","minutes":5}'
                ),
            ),
        )

        first = self.reply(
            text=None,
            finish_reason="tool_calls",
            tool_calls=[tool],
        )

        second = self.reply(
            text="提醒已设置",
        )

        with patch.object(
            agent,
            "create_reminder",
            return_value=(
                "reminder_1",
                datetime(
                    2026, 10, 12,
                    tzinfo=timezone.utc,
                ),
            ),
        ) as reminder:
            with patch.object(
                agent.client.chat.completions,
                "create",
                side_effect=[
                    first,
                    second,
                ],
            ) as model:
                result = agent.chat(
                    "5分钟后提醒我喝水"
                )

        self.assertEqual(
            result,
            "提醒已设置",
        )

        self.assertEqual(
            model.call_count,
            2,
        )

        for call in model.call_args_list:
            self.assertEqual(
                call.kwargs["max_tokens"],
                agent.MAX_OUTPUT_TOKENS,
            )

        reminder.assert_called_once_with(
            "喝水",
            5,
        )


if __name__ == "__main__":
    unittest.main()