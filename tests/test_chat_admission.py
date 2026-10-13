"""统一聊天准入的原子性测试，不调用任何模型。"""

import sqlite3
import unittest

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from src.core.ai_budget import (
    BudgetExceeded,
    init_budget_schema,
)
from src.core.ai_pricing import PricingUnavailable
from src.core.chat_admission import admit_chat_request
from src.core.chat_ownership import (
    RequestOwnerMismatch,
    migrate_chat_ownership,
    update_owned_status,
)
from src.core.chat_quota import (
    DAILY_REQUEST_LIMITS,
    ChatConcurrencyLimitExceeded,
    ChatDailyLimitExceeded,
)


class TestChatAdmission(unittest.TestCase):

    NOW = datetime(
        2026, 10, 12, 12, 0,
        tzinfo=timezone.utc,
    )

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "test_chat.db"

        with closing(self.connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE chat_requests (
                    request_id TEXT PRIMARY KEY,
                    message_hash TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK(status IN (
                            'processing',
                            'completed',
                            'unknown'
                        )),
                    reply TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

            migrate_chat_ownership(conn)
            init_budget_schema(conn)

    def tearDown(self):
        self.temp.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def admit(
        self,
        request_id,
        user="alice",
        role="owner",
        model="deepseek-flash",
    ):
        with closing(self.connect()) as conn:
            return admit_chat_request(
                conn,
                request_id=request_id,
                digest="digest-123",
                user_id=user,
                role=role,
                provider_host="api.deepseek.com",
                model=model,
                now=self.NOW,
            )

    def finish(self, request_id, user="alice"):
        with closing(self.connect()) as conn:
            return update_owned_status(
                conn,
                request_id,
                user,
                "completed",
                "模拟回复",
                self.NOW.isoformat(),
            )

    def counts(self):
        with closing(self.connect()) as conn:
            chat_count = conn.execute(
                "SELECT COUNT(*) FROM chat_requests"
            ).fetchone()[0]

            budget_count = conn.execute(
                "SELECT COUNT(*) FROM ai_budget_reservations"
            ).fetchone()[0]

        return chat_count, budget_count

    def test_new_request_creates_both_records(self):
        result = self.admit("req_001")

        self.assertTrue(result["admitted"])
        self.assertEqual(
            self.counts(),
            (1, 1),
        )

    def test_duplicate_does_not_reserve_again(self):
        self.assertTrue(
            self.admit("req_001")["admitted"]
        )

        self.assertFalse(
            self.admit("req_001")["admitted"]
        )

        self.assertEqual(
            self.counts(),
            (1, 1),
        )

    def test_other_user_cannot_reuse_request(self):
        self.admit("req_001")

        with self.assertRaises(RequestOwnerMismatch):
            self.admit("req_001", user="bob")

        self.assertEqual(
            self.counts(),
            (1, 1),
        )

    def test_unverified_model_does_not_write(self):
        with self.assertRaises(PricingUnavailable):
            self.admit(
                "req_001",
                model="unverified-model",
            )

        self.assertEqual(
            self.counts(),
            (0, 0),
        )

    def test_concurrency_denial_does_not_reserve(self):
        self.admit("req_001")
        self.admit("req_002")

        with self.assertRaises(
            ChatConcurrencyLimitExceeded
        ):
            self.admit("req_003")

        self.assertEqual(
            self.counts(),
            (2, 2),
        )

    def test_daily_quota_denial_does_not_reserve(self):
        # 读取当前配置，额度调整后无需修改测试。
        limit = DAILY_REQUEST_LIMITS["member"]

        for index in range(limit):
            request_id = f"req_{index:03d}"
            self.admit(
                request_id,
                role="member",
            )
            self.finish(request_id)

        with self.assertRaises(
            ChatDailyLimitExceeded
        ):
            self.admit(
                "req_denied",
                role="member",
            )

        self.assertEqual(
            self.counts(),
            (limit, limit),
        )

    def test_one_dollar_global_budget_denies_21st(
        self,
    ):
        # 这里的 20 是全局预算边界（owner 角色，两个用户各 10 次），
        # 与 member 每日额度无关。
        for index in range(20):
            request_id = f"req_{index:03d}"
            user = (
                "alice" if index % 2 == 0
                else "bob"
            )

            self.admit(request_id, user=user)
            self.finish(request_id, user=user)

        with self.assertRaises(BudgetExceeded):
            self.admit("req_21")

        self.assertEqual(
            self.counts(),
            (20, 20),
        )

    def test_parallel_requests_respect_user_concurrency(
        self,
    ):
        def attempt(index):
            try:
                self.admit(f"parallel_{index}")
                return "admitted"
            except ChatConcurrencyLimitExceeded:
                return "blocked"

        with ThreadPoolExecutor(
            max_workers=6
        ) as executor:
            results = list(
                executor.map(attempt, range(6))
            )

        self.assertEqual(
            results.count("admitted"),
            2,
        )

        self.assertEqual(
            results.count("blocked"),
            4,
        )

        self.assertEqual(
            self.counts(),
            (2, 2),
        )

    def test_failed_budget_insert_rolls_back_chat(
        self,
    ):
        # 人为制造预算表约束冲突：
        # 测试事务必须撤销已经插入的聊天记录。
        with closing(self.connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO ai_budget_reservations (
                    request_id,
                    user_id,
                    reserved_nano_usd,
                    charged_nano_usd,
                    state,
                    created_at
                )
                VALUES (
                    'collision_123',
                    'alice',
                    1,
                    NULL,
                    'reserved',
                    ?
                )
                """,
                (self.NOW.isoformat(),),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.admit("collision_123")

        with closing(self.connect()) as conn:
            row = conn.execute(
                """
                SELECT request_id
                FROM chat_requests
                WHERE request_id = 'collision_123'
                """
            ).fetchone()

        self.assertIsNone(row)


if __name__ == "__main__":
    unittest.main()