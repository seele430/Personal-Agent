"""提醒工具数量和参数安全测试。

所有 AI 响应均为模拟数据，不调用真实模型，
也不会创建真实提醒。
"""

import json
import unittest

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from src import agent


class TestAgentToolLimits(unittest.TestCase):

    def setUp(self):
        self.init_patch = patch.object(
            agent,
            "init_db",
        )
        self.init_patch.start()
        self.addCleanup(self.init_patch.stop)

    @staticmethod
    def make_tool(
        message="喝水",
        minutes=5,
        call_id="tool_one",
        raw=None,
    ):
        arguments = (
            raw
            if raw is not None
            else json.dumps(
                {
                    "message": message,
                    "minutes": minutes,
                },
                ensure_ascii=False,
            )
        )

        return SimpleNamespace(
            id=call_id,
            function=SimpleNamespace(
                name="create_reminder",
                arguments=arguments,
            ),
        )

    @staticmethod
    def make_response(
        text="模拟回复",
        tools=None,
    ):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=(
                        "tool_calls" if tools else "stop"
                    ),
                    message=SimpleNamespace(
                        content=text,
                        tool_calls=tools,
                    ),
                ),
            ],
            usage=None,
        )

    # ======================================
    # 1. 多条提醒必须全部拒绝
    # ======================================

    def test_multiple_tools_create_no_reminders(self):
        first = self.make_response(
            text=None,
            tools=[
                self.make_tool("喝水"),
                self.make_tool(
                    "休息",
                    call_id="tool_two",
                ),
            ],
        )

        with patch.object(
            agent.client.chat.completions,
            "create",
            return_value=first,
        ) as model:

            with patch.object(
                agent,
                "create_reminder",
            ) as reminder:

                with self.assertRaises(RuntimeError):
                    agent.chat("提醒我喝水和休息")

        reminder.assert_not_called()
        model.assert_called_once()

    # ======================================
    # 2. 正常单条提醒仍可创建
    # ======================================

    def test_one_valid_reminder_still_works(self):
        first = self.make_response(
            text=None,
            tools=[self.make_tool()],
        )

        second = self.make_response(
            text="提醒已设置",
        )

        with patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=[first, second],
        ) as model:

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

                result = agent.chat(
                    "5分钟后提醒我喝水"
                )

        self.assertEqual(result, "提醒已设置")
        self.assertEqual(model.call_count, 2)

        reminder.assert_called_once_with(
            "喝水",
            5,
        )

    # ======================================
    # 3. 提醒内容过长不能创建
    # ======================================

    def test_long_reminder_text_not_created(self):
        long_message = "a" * (
            agent.MAX_REMINDER_MESSAGE_BYTES + 1
        )

        first = self.make_response(
            text=None,
            tools=[
                self.make_tool(
                    message=long_message,
                ),
            ],
        )

        second = self.make_response(
            text="提醒内容太长",
        )

        with patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=[first, second],
        ) as model:

            with patch.object(
                agent,
                "create_reminder",
            ) as reminder:

                result = agent.chat(
                    "创建一条长提醒"
                )

        reminder.assert_not_called()

        self.assertEqual(
            result,
            "提醒内容太长",
        )

        self.assertEqual(
            model.call_count,
            2,
        )

        tool_result = json.loads(
            model.call_args_list[1]
            .kwargs["messages"][-1]["content"]
        )

        self.assertFalse(tool_result["success"])

    # ======================================
    # 4. 工具参数超过字节上限直接拒绝
    # ======================================

    def test_oversized_tool_arguments_rejected(self):
        first = self.make_response(
            text=None,
            tools=[
                self.make_tool(
                    raw="x" * (
                        agent.MAX_TOOL_ARGUMENT_BYTES + 1
                    ),
                ),
            ],
        )

        with patch.object(
            agent.client.chat.completions,
            "create",
            return_value=first,
        ) as model:

            with patch.object(
                agent,
                "create_reminder",
            ) as reminder:

                with self.assertRaises(RuntimeError):
                    agent.chat("测试超大工具参数")

        model.assert_called_once()
        reminder.assert_not_called()

    # ======================================
    # 5. 工具参数不是对象时不能创建
    # ======================================

    def test_non_object_arguments_fail_safely(self):
        first = self.make_response(
            text=None,
            tools=[
                self.make_tool(
                    raw="[]",
                ),
            ],
        )

        second = self.make_response(
            text="提醒参数无效",
        )

        with patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=[first, second],
        ) as model:

            with patch.object(
                agent,
                "create_reminder",
            ) as reminder:

                result = agent.chat(
                    "测试不合法的工具参数"
                )

        reminder.assert_not_called()

        self.assertEqual(
            result,
            "提醒参数无效",
        )

        self.assertEqual(
            model.call_count,
            2,
        )


if __name__ == "__main__":
    unittest.main()