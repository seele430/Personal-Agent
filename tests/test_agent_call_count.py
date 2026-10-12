
"""测试每次聊天实际收到多少个模型响应。

不调用真实 AI，不写入正式用量数据库。
"""

import unittest

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from src import agent
from src.core.ai_usage import AIUsageStore


class TestAgentCallCount(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()

        self.store = AIUsageStore(
            Path(self.temp.name) / "usage_test.db"
        )

        self.init_patch = patch.object(
            agent,
            "init_db",
        )

        self.store_patch = patch.object(
            agent,
            "_get_usage_store",
            return_value=self.store,
        )

        self.init_patch.start()
        self.store_patch.start()

    def tearDown(self):
        self.store_patch.stop()
        self.init_patch.stop()

        self.temp.cleanup()

    @staticmethod
    def response(
        text="模拟回复",
        *,
        tool_calls=None,
        finish_reason="stop",
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

    @staticmethod
    def reminder_tool():
        return SimpleNamespace(
            id="tool_1",
            function=SimpleNamespace(
                name="create_reminder",
                arguments=(
                    '{"message":"喝水","minutes":5}'
                ),
            ),
        )

    # ======================================
    # 1. 普通聊天：一次响应
    # ======================================

    def test_normal_chat_counts_one_response(self):

        with patch.object(
            agent.client.chat.completions,
            "create",
            return_value=self.response(),
        ) as model:

            with agent.track_chat_usage(
                "alice",
                "request_normal",
            ) as scope:

                result = agent.chat("你好")

        self.assertEqual(result, "模拟回复")

        model.assert_called_once()

        self.assertEqual(
            scope.completed_calls,
            1,
        )

        self.assertEqual(
            self.store.user_summary("alice")["calls"],
            1,
        )

    # ======================================
    # 2. 提醒：两次响应
    # ======================================

    def test_reminder_counts_two_responses(self):

        first = self.response(
            text=None,
            tool_calls=[
                self.reminder_tool()
            ],
            finish_reason="tool_calls",
        )

        second = self.response(
            text="提醒已设置",
        )

        with patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=[
                first,
                second,
            ],
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
            ):

                with agent.track_chat_usage(
                    "alice",
                    "request_reminder",
                ) as scope:

                    result = agent.chat(
                        "5分钟后提醒我喝水"
                    )

        self.assertEqual(
            result,
            "提醒已设置",
        )

        self.assertEqual(model.call_count, 2)

        self.assertEqual(
            scope.completed_calls,
            2,
        )

        self.assertEqual(
            self.store.user_summary("alice")["calls"],
            2,
        )

    # ======================================
    # 3. 第一次模型超时，没有已知响应
    # ======================================

    def test_first_call_timeout_counts_zero(
        self,
    ):
        with patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=TimeoutError(
                "模拟网络超时"
            ),
        ):

            with agent.track_chat_usage(
                "alice",
                "request_timeout",
            ) as scope:

                with self.assertRaises(
                    TimeoutError
                ):
                    agent.chat("你好")

        self.assertEqual(
            scope.completed_calls,
            0,
        )

        self.assertEqual(
            self.store.user_summary("alice")["calls"],
            0,
        )

    # ======================================
    # 4. 第二次模型超时，不能按一次完成结算
    # ======================================

    def test_second_call_timeout_keeps_partial_count(
        self,
    ):
        first = self.response(
            text=None,
            tool_calls=[
                self.reminder_tool()
            ],
            finish_reason="tool_calls",
        )

        with patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=[
                first,
                TimeoutError(
                    "第二次调用超时"
                ),
            ],
        ):

            with patch.object(
                agent,
                "create_reminder",
                return_value=(
                    "reminder_2",
                    datetime(
                        2026, 10, 12,
                        tzinfo=timezone.utc,
                    ),
                ),
            ):

                with agent.track_chat_usage(
                    "alice",
                    "request_partial",
                ) as scope:

                    with self.assertRaises(
                        TimeoutError
                    ):
                        agent.chat(
                            "5分钟后提醒我喝水"
                        )

        self.assertEqual(
            scope.completed_calls,
            1,
        )

        # 已知只有第一次用量。
        # 由于整个 chat() 抛出了异常，
        # 正式接口不得按这一条数据释放预算。
        self.assertEqual(
            self.store.user_summary("alice")["calls"],
            1,
        )

    # ======================================
    # 5. 用量写入失败仍然计数
    # ======================================

    def test_usage_write_failure_still_counts_response(
        self,
    ):
        with patch.object(
            agent.client.chat.completions,
            "create",
            return_value=self.response(),
        ):

            with patch.object(
                agent,
                "_get_usage_store",
                side_effect=OSError(
                    "模拟用量数据库不可写"
                ),
            ):

                with patch.object(
                    agent.logger,
                    "exception",
                ) as log:

                    with agent.track_chat_usage(
                        "alice",
                        "request_write_failure",
                    ) as scope:

                        result = agent.chat("你好")

        self.assertEqual(
            result,
            "模拟回复",
        )

        self.assertEqual(
            scope.completed_calls,
            1,
        )

        # 计数为一但数据库没有记录。
        # 核对器必须将这次费用视为未知。
        self.assertEqual(
            self.store.user_summary("alice")["calls"],
            0,
        )

        log.assert_called_once()

    # ======================================
    # 6. 响应被截断：计数但不能正常结算
    # ======================================

    def test_truncated_response_is_counted_but_rejected(
        self,
    ):
        with patch.object(
            agent.client.chat.completions,
            "create",
            return_value=self.response(
                finish_reason="length",
            ),
        ):

            with agent.track_chat_usage(
                "alice",
                "request_truncated",
            ) as scope:

                with self.assertRaises(
                    RuntimeError
                ):
                    agent.chat("测试长回复")

        self.assertEqual(
            scope.completed_calls,
            1,
        )

        self.assertEqual(
            self.store.user_summary("alice")["calls"],
            1,
        )


if __name__ == "__main__":
    unittest.main()