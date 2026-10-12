
"""AI 预算预留与并发安全测试。"""

import sqlite3
import unittest

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from src.core.ai_budget import (
    BudgetConflict,
    BudgetExceeded,
    budget_usage,
    init_budget_schema,
    reserve_budget,
    settle_budget,
)


class TestAIBudget(unittest.TestCase):

    NOW = datetime(
        2026, 10, 12, 12,
        tzinfo=timezone.utc,
    )

    def setUp(self):
        self.temp = TemporaryDirectory()

        self.path = (
            Path(self.temp.name)
            / "test_budget.db"
        )

        with closing(self.connect()) as conn:
            init_budget_schema(conn)

    def tearDown(self):
        self.temp.cleanup()

    def connect(self):
        return sqlite3.connect(
            self.path,
            timeout=10,
        )

    def reserve(
        self,
        name,
        user="alice",
        cost="0.04",
        cap="0.10",
        global_cap="1",
        now=None,
    ):
        with closing(self.connect()) as conn:

            return reserve_budget(
                conn,
                request_id=name,
                user_id=user,
                reserve_usd=cost,
                user_limit_usd=cap,
                global_limit_usd=global_cap,
                now=(
                    self.NOW
                    if now is None
                    else now
                ),
            )

    def settle(
        self,
        name,
        user="alice",
        actual=None,
    ):
        with closing(self.connect()) as conn:
            return settle_budget(
                conn,
                request_id=name,
                user_id=user,
                actual_usd=actual,
            )

    def usage(
        self,
        user="alice",
        now=None,
    ):
        with closing(self.connect()) as conn:

            return budget_usage(
                conn,
                user_id=user,
                now=(
                    self.NOW
                    if now is None
                    else now
                ),
            )

    # ======================================
    # 1. 预留金额占用预算
    # ======================================

    def test_reserve_counts_against_limit(self):

        self.assertTrue(
            self.reserve("one")["admitted"]
        )

        self.reserve("two")

        with self.assertRaises(BudgetExceeded):
            self.reserve("three")

        self.assertEqual(
            self.usage()["accounted_usd"],
            "0.08",
        )

    # ======================================
    # 2. 相同 request_id 不重复预留
    # ======================================

    def test_duplicate_does_not_reserve_twice(self):

        self.reserve("one")

        self.assertFalse(
            self.reserve("one")["admitted"]
        )

        self.assertEqual(
            self.usage()["requests"],
            1,
        )

    # ======================================
    # 3. 其他用户不能冒领请求
    # ======================================

    def test_conflicting_owner_or_estimate_rejected(
        self,
    ):
        self.reserve("one")

        with self.assertRaises(BudgetConflict):
            self.reserve(
                "one",
                user="bob",
            )

        with self.assertRaises(BudgetConflict):
            self.reserve(
                "one",
                cost="0.03",
            )

    # ======================================
    # 4. 全站预算由所有用户共享
    # ======================================

    def test_global_limit_shared_across_users(self):

        self.reserve(
            "one",
            user="alice",
            global_cap="0.05",
        )

        with self.assertRaises(BudgetExceeded):
            self.reserve(
                "two",
                user="bob",
                global_cap="0.05",
            )

    # ======================================
    # 5. 实际费用较低时释放差额
    # ======================================

    def test_settlement_releases_unused_reserve(
        self,
    ):
        self.reserve(
            "one",
            cost="0.08",
        )

        self.assertEqual(
            self.settle(
                "one",
                actual="0.01",
            ),
            "settled",
        )

        self.reserve(
            "two",
            cost="0.08",
        )

        self.assertEqual(
            self.usage()["accounted_usd"],
            "0.09",
        )

    # ======================================
    # 6. 未知费用保留整个预留额
    # ======================================

    def test_unknown_keeps_full_reserve(self):

        self.reserve(
            "one",
            cost="0.08",
        )

        self.assertEqual(
            self.settle("one"),
            "uncertain",
        )

        with self.assertRaises(BudgetExceeded):
            self.reserve(
                "two",
                cost="0.03",
            )

    # ======================================
    # 7. 超出预留费用必须报告
    # ======================================

    def test_overrun_counted_and_reported(self):

        self.reserve(
            "one",
            cost="0.04",
        )

        self.assertEqual(
            self.settle(
                "one",
                actual="0.12",
            ),
            "overrun",
        )

        self.assertEqual(
            self.usage()["accounted_usd"],
            "0.12",
        )

        with self.assertRaises(BudgetExceeded):
            self.reserve("two")

    # ======================================
    # 8. 结算幂等，不能随意改写
    # ======================================

    def test_settlement_idempotent_but_not_mutable(
        self,
    ):
        self.reserve("one")

        self.assertEqual(
            self.settle(
                "one",
                actual="0.02",
            ),
            "settled",
        )

        self.assertEqual(
            self.settle(
                "one",
                actual="0.02",
            ),
            "settled",
        )

        with self.assertRaises(BudgetConflict):
            self.settle(
                "one",
                actual="0.03",
            )

    # ======================================
    # 9. 滚动 24 小时窗口
    # ======================================

    def test_24_hour_rolling_window(self):

        self.reserve(
            "one",
            cost="0.08",
        )

        future = (
            self.NOW
            + timedelta(hours=25)
        )

        self.reserve(
            "two",
            cost="0.08",
            now=future,
        )

        self.assertEqual(
            self.usage(now=future)["requests"],
            1,
        )

    # ======================================
    # 10. 并发预留不能突破预算
    # ======================================

    def test_concurrent_reservations_never_exceed_cap(
        self,
    ):

        def work(index):
            try:
                self.reserve(
                    f"req_{index}",
                    cost="0.04",
                )

                return "ok"

            except BudgetExceeded:
                return "denied"

        with ThreadPoolExecutor(
            max_workers=6,
        ) as pool:

            results = list(
                pool.map(
                    work,
                    range(8),
                )
            )

        self.assertEqual(
            results.count("ok"),
            2,
        )

        self.assertEqual(
            results.count("denied"),
            6,
        )

    # ======================================
    # 11. 非法金额和不存在的请求
    # ======================================

    def test_missing_record_and_invalid_money_rejected(
        self,
    ):
        with self.assertRaises(BudgetConflict):
            self.settle("missing")

        for bad in (
            "-1",
            "NaN",
            True,
            1.0,
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.reserve(
                        "invalid",
                        cost=bad,
                    )

        self.assertEqual(
            self.usage()["requests"],
            0,
        )

    # ======================================
    # 12. 初始化可以重复执行
    # ======================================

    def test_schema_repeated_init_safe(self):

        with closing(self.connect()) as conn:
            init_budget_schema(conn)
            init_budget_schema(conn)

        self.assertEqual(
            self.usage()["requests"],
            0,
        )


if __name__ == "__main__":
    unittest.main()