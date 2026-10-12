
"""AI 预算用量核对与结算测试。

只操作临时 SQLite 数据库，不调用 DeepSeek。
"""

import sqlite3
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from src.core.ai_budget import (
    BudgetConflict,
    init_budget_schema,
    reserve_budget,
)
from src.core.ai_budget_reconcile import (
    observed_request_cost,
    reconcile_recorded_request,
)
from src.core.ai_usage import AIUsageStore


HOST = "api.deepseek.com"
MODEL = "deepseek-flash"


class TestBudgetReconcile(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)

        self.usage_path = root / "usage.db"
        self.budget_path = root / "budget.db"

        self.usage = AIUsageStore(self.usage_path)

        with closing(self.connect_budget()) as conn:
            init_budget_schema(conn)

    def tearDown(self):
        self.temp.cleanup()

    def connect_budget(self):
        return sqlite3.connect(
            self.budget_path,
            timeout=10,
        )

    def record(
        self,
        request_id="req_one",
        *,
        user="alice",
        number=1,
        prompt=1000,
        completion=500,
        cached=0,
        model=MODEL,
        total=None,
        missing_usage=False,
    ):
        if missing_usage:
            usage = None
        else:
            usage = {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": (
                    prompt + completion
                    if total is None
                    else total
                ),
            }

            if cached is not None:
                usage["prompt_cache_hit_tokens"] = cached

        return self.usage.record_completion(
            user_id=user,
            request_id=request_id,
            call_number=number,
            model=model,
            response={"usage": usage},
        )

    def quote(
        self,
        request_id="req_one",
        *,
        user="alice",
        count=1,
        host=HOST,
        model=MODEL,
    ):
        return observed_request_cost(
            usage_db_path=self.usage_path,
            request_id=request_id,
            user_id=user,
            expected_calls=count,
            provider_host=host,
            model=model,
        )

    def reserve(self, request_id="req_one", user="alice"):
        with closing(self.connect_budget()) as conn:
            reserve_budget(
                conn,
                request_id=request_id,
                user_id=user,
                reserve_usd="0.05",
                user_limit_usd="1",
                global_limit_usd="1",
            )

    def reconcile(
        self,
        request_id="req_one",
        *,
        user="alice",
        completed=True,
        count=1,
    ):
        return reconcile_recorded_request(
            budget_db_path=self.budget_path,
            usage_db_path=self.usage_path,
            request_id=request_id,
            user_id=user,
            provider_host=HOST,
            model=MODEL,
            completed=completed,
            expected_calls=count,
        )

    def budget_row(self, request_id="req_one"):
        with closing(self.connect_budget()) as conn:
            return conn.execute(
                """
                SELECT state, charged_nano_usd
                FROM ai_budget_reservations
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()

    # 1. 普通聊天的费用可核对

    def test_single_call_quote(self):
        self.record()

        self.assertEqual(
            self.quote(),
            Decimal("0.0009"),
        )

    # 2. 提醒的两次调用必须全部计入

    def test_two_calls_are_added(self):
        self.record(number=1)
        self.record(number=2)

        self.assertEqual(
            self.quote(count=2),
            Decimal("0.0018"),
        )

    # 3. 两次调用少了一条，不能提前释放预算

    def test_missing_second_call_is_unknown(self):
        self.record(number=1)

        self.assertIsNone(
            self.quote(count=2)
        )

    # 4. 意外出现额外调用，也不能随意结算

    def test_extra_call_is_unknown(self):
        self.record(number=1)
        self.record(number=2)

        self.assertIsNone(
            self.quote(count=1)
        )

    # 5. 缺少 usage 不能按零费用结算

    def test_missing_usage_is_unknown(self):
        self.record(missing_usage=True)

        self.assertIsNone(
            self.quote()
        )

    # 6. Token 总数矛盾，停止计价

    def test_inconsistent_usage_is_unknown(self):
        self.record(total=9999)

        self.assertIsNone(
            self.quote()
        )

    # 7. 缓存字段缺失，按较贵费率估算

    def test_unknown_cache_uses_miss_rate(self):
        self.record(cached=None)

        self.assertEqual(
            self.quote(),
            Decimal("0.0009"),
        )

    # 8. 不同模型不能混用报价

    def test_unverified_model_is_unknown(self):
        self.record(model="other-model")

        self.assertIsNone(
            self.quote()
        )

    # 9. 用户归属必须匹配

    def test_wrong_user_cannot_quote_usage(self):
        self.record(user="alice")

        self.assertIsNone(
            self.quote(user="bob")
        )

    # 10. 完整用量可结算并释放差额

    def test_complete_usage_settles_budget(self):
        self.reserve()
        self.record()

        state = self.reconcile()

        self.assertEqual(state, "settled")

        row = self.budget_row()

        self.assertEqual(row[0], "settled")

        # US$0.0009 = 900000 nano-USD
        self.assertEqual(row[1], 900_000)

        # 相同结果再次结算应保持幂等。
        self.assertEqual(
            self.reconcile(),
            "settled",
        )

    # 11. 用量不完整，保留全部 US$0.05

    def test_incomplete_usage_keeps_reserve(self):
        self.reserve()
        self.record(number=1)

        state = self.reconcile(count=2)

        self.assertEqual(state, "uncertain")
        self.assertEqual(
            self.budget_row(),
            ("uncertain", 50_000_000),
        )

    # 12. 模型失败不能释放预留额度

    def test_failed_request_keeps_reserve(self):
        self.reserve()
        self.record()

        self.assertEqual(
            self.reconcile(completed=False),
            "uncertain",
        )

        self.assertEqual(
            self.budget_row()[1],
            50_000_000,
        )

    # 13. 其他用户无权结算预算

    def test_other_user_cannot_settle(self):
        self.reserve(user="alice")
        self.record(user="alice")

        with self.assertRaises(BudgetConflict):
            self.reconcile(user="bob")

        self.assertEqual(
            self.budget_row()[0],
            "reserved",
        )

    # 14. 实际费用高于预留额要如实报告

    def test_overrun_is_recorded(self):
        self.reserve()

        self.record(
            prompt=150000,
            completion=50000,
        )

        self.assertEqual(
            self.reconcile(),
            "overrun",
        )

        self.assertGreater(
            self.budget_row()[1],
            50_000_000,
        )


if __name__ == "__main__":
    unittest.main()