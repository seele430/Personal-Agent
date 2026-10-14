"""AI 用量 trace 字段测试。

使用临时数据库，不调用模型，不访问网络。
覆盖旧库迁移、trace 字段写入与读取、非法输入处理。
"""

import sqlite3
import unittest

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from src.core.ai_usage import (
    MAX_FINISH_REASON_CHARS,
    MAX_TOOL_NAMES_CHARS,
    AIUsageStore,
)


# 不含 trace 列的旧版建表语句。
OLD_SCHEMA = """
CREATE TABLE ai_usage_calls (
    request_id TEXT NOT NULL,
    call_number INTEGER NOT NULL CHECK(call_number >= 1),
    user_id TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    cache_hit_tokens INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY (request_id, call_number)
);
"""


def fake_response(prompt=100, completion=20, total=120):
    return SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            prompt_cache_hit_tokens=0,
        )
    )


class TestAIUsageTrace(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "usage_trace_test.db"

    def tearDown(self):
        self.temp.cleanup()

    def store(self):
        return AIUsageStore(self.path)

    def columns(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(ai_usage_calls)"
                )
            }

    def record(self, store, **kwargs):
        payload = {
            "user_id": "u1",
            "request_id": "r1",
            "call_number": 1,
            "model": "deepseek-flash",
            "response": fake_response(),
        }
        payload.update(kwargs)

        return store.record_completion(**payload)

    # ---------- 迁移 ----------

    def test_new_database_has_trace_columns(self):
        self.store()

        self.assertLessEqual(
            {"latency_ms", "finish_reason", "tool_names"},
            self.columns(),
        )

    def test_old_database_is_migrated_in_place(self):
        # 模拟升级前的库，并写入一行历史数据。
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.executescript(OLD_SCHEMA)
            conn.execute(
                """
                INSERT INTO ai_usage_calls (
                    request_id, call_number, user_id, model,
                    prompt_tokens, completion_tokens,
                    total_tokens, cache_hit_tokens, created_at
                )
                VALUES ('old', 1, 'u1', 'deepseek-flash',
                        10, 2, 12, 0, '2026-10-01T00:00:00+00:00')
                """
            )

        store = self.store()

        self.assertLessEqual(
            {"latency_ms", "finish_reason", "tool_names"},
            self.columns(),
        )

        # 历史行保留，trace 字段为 NULL，不伪造数值。
        rows = store.request_trace("u1", "old")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["total_tokens"], 12)
        self.assertIsNone(rows[0]["latency_ms"])
        self.assertIsNone(rows[0]["finish_reason"])
        self.assertIsNone(rows[0]["tool_names"])

    def test_init_is_idempotent(self):
        self.store()
        store = self.store()

        self.assertTrue(self.record(store))

    # ---------- 写入与读取 ----------

    def test_trace_fields_are_stored(self):
        store = self.store()

        self.record(
            store,
            latency_ms=1234,
            finish_reason="tool_calls",
            tool_names=["create_reminder"],
        )

        row = store.request_trace("u1", "r1")[0]

        self.assertEqual(row["latency_ms"], 1234)
        self.assertEqual(row["finish_reason"], "tool_calls")
        self.assertEqual(row["tool_names"], "create_reminder")

    def test_trace_defaults_to_null(self):
        store = self.store()
        self.record(store)

        row = store.request_trace("u1", "r1")[0]

        self.assertIsNone(row["latency_ms"])
        self.assertIsNone(row["finish_reason"])
        self.assertIsNone(row["tool_names"])

    def test_trace_is_ordered_by_call_number(self):
        store = self.store()

        self.record(store, call_number=2, latency_ms=200)
        self.record(store, call_number=1, latency_ms=100)

        rows = store.request_trace("u1", "r1")

        self.assertEqual(
            [row["call_number"] for row in rows],
            [1, 2],
        )

    def test_trace_does_not_leak_across_users(self):
        store = self.store()
        self.record(store)

        self.assertEqual(store.request_trace("u2", "r1"), [])

    def test_multiple_tool_names_are_joined(self):
        store = self.store()

        self.record(
            store,
            tool_names=("create_reminder", "list_reminders"),
        )

        self.assertEqual(
            store.request_trace("u1", "r1")[0]["tool_names"],
            "create_reminder,list_reminders",
        )

    # ---------- 非法输入 ----------

    def test_invalid_latency_becomes_null(self):
        store = self.store()

        for index, bad in enumerate((-1, 1.5, "900", True, None), start=1):
            self.record(
                store,
                request_id=f"bad_latency_{index}",
                latency_ms=bad,
            )

            row = store.request_trace("u1", f"bad_latency_{index}")[0]

            self.assertIsNone(
                row["latency_ms"],
                f"latency_ms={bad!r} 应记为 NULL",
            )

    def test_long_strings_are_truncated(self):
        store = self.store()

        self.record(
            store,
            finish_reason="x" * 500,
            tool_names=["y" * 500],
        )

        row = store.request_trace("u1", "r1")[0]

        self.assertEqual(
            len(row["finish_reason"]),
            MAX_FINISH_REASON_CHARS,
        )

        self.assertEqual(
            len(row["tool_names"]),
            MAX_TOOL_NAMES_CHARS,
        )

    def test_blank_and_non_string_values_become_null(self):
        store = self.store()

        self.record(
            store,
            request_id="blank",
            finish_reason="   ",
            tool_names=["", "  "],
        )

        row = store.request_trace("u1", "blank")[0]

        self.assertIsNone(row["finish_reason"])
        self.assertIsNone(row["tool_names"])

    def test_request_trace_rejects_empty_identity(self):
        store = self.store()

        with self.assertRaises(ValueError):
            store.request_trace("", "r1")

        with self.assertRaises(ValueError):
            store.request_trace("u1", "  ")


if __name__ == "__main__":
    unittest.main()