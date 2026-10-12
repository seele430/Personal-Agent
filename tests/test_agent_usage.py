
"""验证模型调用入口的 Token 用量记录。

所有模型响应均为模拟值，不调用真实 API。
数据库只使用 TemporaryDirectory。
"""

import unittest

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from src import agent
from src.core.ai_usage import AIUsageStore


class TestAgentModelUsage(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()

        self.store = AIUsageStore(
            Path(self.temp.name) / "test_usage.db"
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
    def reply(
        content="模拟回复",
        tool_calls=None,
        usage=True,
    ):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=content,
                        tool_calls=tool_calls,
                    )
                )
            ],
            usage=(
                SimpleNamespace(
                    prompt_tokens=11,
                    completion_tokens=4,
                    total_tokens=15,
                )
                if usage
                else None
            ),
        )

    # ======================================
    # 1. 普通聊天只记录一次
    # ======================================

    def test_one_call_records_known_usage(self):

        with patch.object(
            agent.client.chat.completions,
            "create",
            return_value=self.reply(),
        ) as sdk:

            with agent.track_chat_usage(
                "alice",
                "request_12345",
            ):
                result = agent.chat("你好")

        self.assertEqual(
            result,
            "模拟回复",
        )

        sdk.assert_called_once()

        summary = self.store.user_summary("alice")

        self.assertEqual(
            summary["calls"],
            1,
        )

        self.assertEqual(
            summary["observed_total_tokens"],
            15,
        )

    # ======================================
    # 2. 提醒功能调用两次模型
    # ======================================

    def test_reminder_records_two_model_calls(self):

        tool_call = SimpleNamespace(
            id="call_one",
            function=SimpleNamespace(
                name="create_reminder",
                arguments=(
                    '{"message":"喝水","minutes":5}'
                ),
            ),
        )

        first = self.reply(
            content=None,
            tool_calls=[tool_call],
        )

        second = self.reply(
            content="提醒已设置",
        )

        with patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=[first, second],
        ) as sdk:

            with patch.object(
                agent,
                "create_reminder",
                return_value=(
                    "rem_1",
                    datetime(
                        2026, 10, 12,
                        tzinfo=timezone.utc,
                    ),
                ),
            ) as reminder:

                with agent.track_chat_usage(
                    "alice",
                    "request_23456",
                ):
                    result = agent.chat(
                        "5分钟后提醒喝水"
                    )

        self.assertEqual(
            result,
            "提醒已设置",
        )

        self.assertEqual(
            sdk.call_count,
            2,
        )

        reminder.assert_called_once_with(
            "喝水",
            5,
        )

        summary = self.store.user_summary("alice")

        self.assertEqual(
            summary["calls"],
            2,
        )

        self.assertEqual(
            summary["observed_total_tokens"],
            30,
        )

    # ======================================
    # 3. 没有用量上下文，不擅自归属用户
    # ======================================

    def test_without_authenticated_context(
        self,
    ):
        with patch.object(
            agent.client.chat.completions,
            "create",
            return_value=self.reply(),
        ):
            self.assertEqual(
                agent.chat("你好"),
                "模拟回复",
            )

        self.assertEqual(
            self.store.user_summary("alice")[
                "calls"
            ],
            0,
        )

    # ======================================
    # 4. 缺少 usage，明确记录为未知
    # ======================================

    def test_missing_usage_is_unknown(self):

        with patch.object(
            agent.client.chat.completions,
            "create",
            return_value=self.reply(
                usage=False,
            ),
        ):

            with agent.track_chat_usage(
                "alice",
                "request_34567",
            ):
                agent.chat("你好")

        self.assertEqual(
            self.store.user_summary("alice")[
                "unreported_calls"
            ],
            1,
        )

    # ======================================
    # 5. 请求结束后不能串号
    # ======================================

    def test_scope_exits_cleanly(self):

        with patch.object(
            agent.client.chat.completions,
            "create",
            return_value=self.reply(),
        ):

            with agent.track_chat_usage(
                "alice",
                "request_45678",
            ):
                agent.chat("第一条")

            agent.chat("不归属的调用")

            with agent.track_chat_usage(
                "bob",
                "request_56789",
            ):
                agent.chat("第二条")

        self.assertEqual(
            self.store.user_summary("alice")[
                "calls"
            ],
            1,
        )

        self.assertEqual(
            self.store.user_summary("bob")[
                "calls"
            ],
            1,
        )

    # ======================================
    # 6. 模型超时不能伪造已知用量
    # ======================================

    def test_model_failure_has_no_known_usage(self):

        with patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=TimeoutError(
                "模拟超时"
            ),
        ):

            with agent.track_chat_usage(
                "alice",
                "request_67890",
            ):

                with self.assertRaises(
                    TimeoutError
                ):
                    agent.chat("你好")

        self.assertEqual(
            self.store.user_summary("alice")[
                "calls"
            ],
            0,
        )

    # ======================================
    # 7. 记账失败不能吞掉已收到的回复
    # ======================================

    def test_usage_write_failure_preserves_reply(
        self,
    ):

        with patch.object(
            agent.client.chat.completions,
            "create",
            return_value=self.reply(),
        ):

            with patch.object(
                agent,
                "_get_usage_store",
                side_effect=OSError(
                    "模拟磁盘不可写"
                ),
            ):

                with patch.object(
                    agent.logger,
                    "exception",
                ) as log:

                    with agent.track_chat_usage(
                        "alice",
                        "request_78901",
                    ):
                        result = agent.chat(
                            "你好"
                        )

        self.assertEqual(
            result,
            "模拟回复",
        )

        log.assert_called_once()

    # ======================================
    # 8. 并发用户不会相互串号
    # ======================================

    def test_parallel_contexts_are_isolated(
        self,
    ):

        with patch.object(
            agent.client.chat.completions,
            "create",
            return_value=self.reply(),
        ):

            def run(index):
                with agent.track_chat_usage(
                    f"user_{index}",
                    f"request_{index}",
                ):
                    return agent.chat(
                        "并发测试"
                    )

            with ThreadPoolExecutor(
                max_workers=4,
            ) as pool:

                results = list(
                    pool.map(
                        run,
                        range(5),
                    )
                )

        self.assertEqual(
            results,
            ["模拟回复"] * 5,
        )

        for index in range(5):
            self.assertEqual(
                self.store.user_summary(
                    f"user_{index}"
                )["calls"],
                1,
            )

    # ======================================
    # 9. 拒绝无效身份
    # ======================================

    def test_rejects_invalid_tracking_identity(
        self,
    ):

        with self.assertRaises(ValueError):
            with agent.track_chat_usage(
                "",
                "request_123",
            ):
                pass

        with self.assertRaises(ValueError):
            with agent.track_chat_usage(
                "alice",
                "",
            ):
                pass


if __name__ == "__main__":
    unittest.main()