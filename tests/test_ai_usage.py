
"""模型用量记录测试。

全部使用临时数据库和模拟 SDK 响应。
"""

import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from src.core.ai_usage import AIUsageStore


class TestAIUsage(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()

        self.db_path = (
            Path(self.temp.name) / "usage.db"
        )

        self.store = AIUsageStore(
            self.db_path
        )

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def response():
        return SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
            )
        )

    def record(
        self,
        request_id="req_one",
        number=1,
        user_id="alice",
        response=None,
    ):
        return self.store.record_completion(
            user_id=user_id,
            request_id=request_id,
            call_number=number,
            model="mock-model",
            response=(
                self.response()
                if response is None
                else response
            ),
        )

    # ======================================
    # 1. 单次调用
    # ======================================

    def test_one_call(self):
        self.assertTrue(self.record())

        result = self.store.user_summary(
            "alice"
        )

        self.assertEqual(
            result["calls"],
            1,
        )

        self.assertEqual(
            result["observed_total_tokens"],
            15,
        )

    # ======================================
    # 2. 一条聊天有两次模型调用
    # ======================================

    def test_two_model_calls_for_one_chat(self):
        self.record(number=1)
        self.record(number=2)

        result = self.store.user_summary(
            "alice"
        )

        self.assertEqual(
            result["calls"],
            2,
        )

        self.assertEqual(
            result["observed_total_tokens"],
            30,
        )

    # ======================================
    # 3. 防止重复记录
    # ======================================

    def test_same_call_not_double_counted(self):
        self.assertTrue(
            self.record()
        )

        self.assertFalse(
            self.record()
        )

        self.assertEqual(
            self.store.user_summary("alice")["calls"],
            1,
        )

    # ======================================
    # 4. 不同用户独立
    # ======================================

    def test_users_are_independent(self):
        self.record(user_id="alice")

        self.record(
            request_id="req_two",
            user_id="bob",
        )

        self.assertEqual(
            self.store.user_summary("alice")["calls"],
            1,
        )

        self.assertEqual(
            self.store.user_summary("bob")["calls"],
            1,
        )

        self.assertEqual(
            self.store.user_summary("empty")["calls"],
            0,
        )

    # ======================================
    # 5. 缺失 usage 不等于免费
    # ======================================

    def test_missing_usage_not_assumed_free(self):
        self.record(
            response=SimpleNamespace(
                usage=None
            )
        )

        result = self.store.user_summary(
            "alice"
        )

        self.assertEqual(
            result["unreported_calls"],
            1,
        )

        self.assertEqual(
            result["observed_total_tokens"],
            0,
        )

    # ======================================
    # 6. 不完整 usage 保留未知标记
    # ======================================

    def test_partial_usage_is_unreported(self):
        self.record(
            response={
                "usage": {
                    "prompt_tokens": 9
                }
            }
        )

        result = self.store.user_summary(
            "alice"
        )

        self.assertEqual(
            result["observed_prompt_tokens"],
            9,
        )

        self.assertEqual(
            result["unreported_calls"],
            1,
        )

    # ======================================
    # 7. 兼容缓存命中数据
    # ======================================

    def test_deepseek_cache_hit_tokens(self):
        self.record(
            response={
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 3,
                    "total_tokens": 15,
                    "prompt_cache_hit_tokens": 8,
                }
            }
        )

        self.assertEqual(
            self.store.user_summary("alice")[
                "observed_cache_hit_tokens"
            ],
            8,
        )

    # ======================================
    # 8. 兼容另一种缓存用量结构
    # ======================================

    def test_openai_style_cached_tokens(self):
        self.record(
            response={
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 3,
                    "total_tokens": 15,
                    "prompt_tokens_details": {
                        "cached_tokens": 7,
                    },
                }
            }
        )

        self.assertEqual(
            self.store.user_summary("alice")[
                "observed_cache_hit_tokens"
            ],
            7,
        )

    # ======================================
    # 9. 重建对象后数据仍存在
    # ======================================

    def test_reopening_database_keeps_records(self):
        self.record()

        reopened = AIUsageStore(
            self.db_path
        )

        self.assertEqual(
            reopened.user_summary("alice")["calls"],
            1,
        )

    # ======================================
    # 10. 无效身份和调用序号拒绝记录
    # ======================================

    def test_invalid_identity_and_call_number(self):
        cases = (
            ("", "req_one", 1),
            ("alice", "", 1),
            ("alice", "req_one", 0),
            ("alice", "req_one", True),
        )

        for user_id, request_id, number in cases:
            with self.subTest(
                user_id=user_id,
                number=number,
            ):
                with self.assertRaises(ValueError):
                    self.record(
                        request_id,
                        number,
                        user_id,
                    )

        self.assertEqual(
            self.store.user_summary("alice")["calls"],
            0,
        )


if __name__ == "__main__":
    unittest.main()