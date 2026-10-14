"""验证模型调用入口写入 trace 字段。

所有模型响应均为模拟值，不调用真实 API。
数据库只使用 TemporaryDirectory。

重点：
- latency_ms / finish_reason / tool_names 来自真实调用路径
- 只记录工具名，不记录工具参数（参数含用户输入内容）
- 调用被判定为异常时，trace 仍然留下记录
"""

import unittest

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from src import agent
from src.core.ai_usage import AIUsageStore


REMINDER_TEXT = "喝水"

TOOL_CALL = SimpleNamespace(
    id="call_one",
    function=SimpleNamespace(
        name="create_reminder",
        arguments=(
            '{"message":"' + REMINDER_TEXT + '","minutes":5}'
        ),
    ),
)


def reply(
    content="模拟回复",
    tool_calls=None,
    finish_reason="stop",
    choices=True,
):
    return SimpleNamespace(
        choices=(
            [
                SimpleNamespace(
                    finish_reason=finish_reason,
                    message=SimpleNamespace(
                        content=content,
                        tool_calls=tool_calls,
                    ),
                )
            ]
            if choices
            else []
        ),
        usage=SimpleNamespace(
            prompt_tokens=11,
            completion_tokens=4,
            total_tokens=15,
        ),
    )


class TestAgentUsageTrace(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "test_usage.db"

        self.store = AIUsageStore(self.db_path)

        self.init_patch = patch.object(agent, "init_db")

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

    def trace(self, request_id, user_id="alice"):
        return self.store.request_trace(user_id, request_id)

    # ======================================
    # 1. 普通聊天记录耗时与结束原因
    # ======================================

    def test_normal_chat_records_trace(self):

        with patch.object(
            agent.client.chat.completions,
            "create",
            return_value=reply(),
        ):
            with agent.track_chat_usage("alice", "request_trace_1"):
                agent.chat("你好")

        rows = self.trace("request_trace_1")

        self.assertEqual(len(rows), 1)

        row = rows[0]

        self.assertEqual(row["finish_reason"], "stop")

        # 未调用工具时不伪造空字符串。
        self.assertIsNone(row["tool_names"])

        self.assertIsInstance(row["latency_ms"], int)
        self.assertGreaterEqual(row["latency_ms"], 0)

    # ======================================
    # 2. 提醒路径按调用顺序记录工具名
    # ======================================

    def test_reminder_path_records_tool_name(self):

        with patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=[
                reply(
                    content=None,
                    tool_calls=[TOOL_CALL],
                    finish_reason="tool_calls",
                ),
                reply(content="提醒已设置"),
            ],
        ):
            with patch.object(
                agent,
                "create_reminder",
                return_value=(
                    "rem_1",
                    datetime(2026, 10, 12, tzinfo=timezone.utc),
                ),
            ):
                with agent.track_chat_usage(
                    "alice",
                    "request_trace_2",
                ):
                    agent.chat("5分钟后提醒喝水")

        rows = self.trace("request_trace_2")

        self.assertEqual(len(rows), 2)

        self.assertEqual(rows[0]["call_number"], 1)
        self.assertEqual(rows[0]["finish_reason"], "tool_calls")
        self.assertEqual(rows[0]["tool_names"], "create_reminder")

        # 第二次调用只生成回复，不再调工具。
        self.assertEqual(rows[1]["call_number"], 2)
        self.assertEqual(rows[1]["finish_reason"], "stop")
        self.assertIsNone(rows[1]["tool_names"])

    # ======================================
    # 3. 工具参数不得落库
    # ======================================

    def test_tool_arguments_are_not_stored(self):

        with patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=[
                reply(
                    content=None,
                    tool_calls=[TOOL_CALL],
                    finish_reason="tool_calls",
                ),
                reply(content="提醒已设置"),
            ],
        ):
            with patch.object(
                agent,
                "create_reminder",
                return_value=(
                    "rem_1",
                    datetime(2026, 10, 12, tzinfo=timezone.utc),
                ),
            ):
                with agent.track_chat_usage(
                    "alice",
                    "request_trace_3",
                ):
                    agent.chat("5分钟后提醒喝水")

        # 直接扫描数据库文件：用户输入的提醒内容不能出现在用量库中。
        raw = self.db_path.read_bytes()

        self.assertNotIn(REMINDER_TEXT.encode("utf-8"), raw)
        self.assertNotIn(b'"minutes"', raw)

    # ======================================
    # 4. 输出超长：先记 trace 再报错
    # ======================================

    def test_length_finish_reason_is_recorded_before_raising(self):

        with patch.object(
            agent.client.chat.completions,
            "create",
            return_value=reply(finish_reason="length"),
        ):
            with agent.track_chat_usage("alice", "request_trace_4"):
                with self.assertRaises(RuntimeError):
                    agent.chat("你好")

        rows = self.trace("request_trace_4")

        # 调用已经发生并计费，必须留下记录。
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["finish_reason"], "length")
        self.assertEqual(rows[0]["total_tokens"], 15)

    # ======================================
    # 5. 没有 choices：仍然留下记录
    # ======================================

    def test_missing_choices_is_recorded_before_raising(self):

        with patch.object(
            agent.client.chat.completions,
            "create",
            return_value=reply(choices=False),
        ):
            with agent.track_chat_usage("alice", "request_trace_5"):
                with self.assertRaises(RuntimeError):
                    agent.chat("你好")

        rows = self.trace("request_trace_5")

        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["finish_reason"])
        self.assertIsNone(rows[0]["tool_names"])
        self.assertEqual(rows[0]["total_tokens"], 15)


if __name__ == "__main__":
    unittest.main()