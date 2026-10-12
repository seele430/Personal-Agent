
"""用临时数据库测试聊天次数和并发上限，不调用付费模型。"""

import sqlite3
import unittest

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from src.core.chat_ownership import (
    RequestMessageConflict,
    RequestOwnerMismatch,
    migrate_chat_ownership,
    update_owned_status,
)

from src.core.chat_quota import (
    ChatConcurrencyLimitExceeded,
    ChatDailyLimitExceeded,
    claim_limited_request,
    prepare_chat_quota,
)


class TestChatQuota(unittest.TestCase):

    NOW = datetime(
        2026, 10, 12, 12, 0,
        tzinfo=timezone.utc,
    )

    def setUp(self):
        self.temp_dir = TemporaryDirectory()

        self.db_path = (
            Path(self.temp_dir.name)
            / "chat_test.db"
        )

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
            prepare_chat_quota(conn)

    def tearDown(self):
        self.temp_dir.cleanup()

    def connect(self):
        conn = sqlite3.connect(
            self.db_path,
            timeout=10,
        )

        conn.row_factory = sqlite3.Row
        return conn

    def claim(
        self,
        request_id,
        user_id="alice",
        *,
        digest="hash1",
        now=None,
        daily=2,
        per_user=2,
        global_active=4,
        role="member",
    ):

        daily_limits = {
            name: daily
            for name in (
                "member",
                "trusted",
                "admin",
                "owner",
            )
        }

        with closing(self.connect()) as conn:
            return claim_limited_request(
                conn,
                request_id,
                digest,
                user_id,
                role,
                now=(
                    self.NOW
                    if now is None
                    else now
                ),
                daily_limits=daily_limits,
                user_concurrent_limit=per_user,
                global_concurrent_limit=global_active,
            )

    def finish(
        self,
        request_id,
        *,
        user_id="alice",
        status="completed",
    ):

        with closing(self.connect()) as conn:
            return update_owned_status(
                conn,
                request_id,
                user_id,
                status,
                "模拟回复",
                self.NOW.isoformat(),
            )

    # 1. 重复请求不能多占额度

    def test_duplicate_does_not_use_extra_quota(self):
        self.assertIsNone(
            self.claim("req_1", daily=1)
        )

        self.assertEqual(
            self.claim("req_1", daily=1)["status"],
            "processing",
        )

        self.assertTrue(self.finish("req_1"))

        self.assertEqual(
            self.claim("req_1", daily=1)["reply"],
            "模拟回复",
        )

        with self.assertRaises(
            ChatDailyLimitExceeded
        ):
            self.claim("req_2", daily=1)

    # 2. 结果不明仍然计入请求次数

    def test_unknown_counts_toward_limit(self):
        self.claim("req_1", daily=1)

        self.finish(
            "req_1",
            status="unknown",
        )

        with self.assertRaises(
            ChatDailyLimitExceeded
        ):
            self.claim("req_2", daily=1)

    # 3. 单用户并发限制

    def test_user_concurrency_recovers_after_completion(
        self,
    ):
        self.claim(
            "req_1",
            per_user=1,
        )

        with self.assertRaises(
            ChatConcurrencyLimitExceeded
        ):
            self.claim(
                "req_2",
                per_user=1,
            )

        self.finish("req_1")

        self.assertIsNone(
            self.claim(
                "req_2",
                per_user=1,
            )
        )

    # 4. 全局并发限制

    def test_global_concurrency_applies_to_multiple_users(
        self,
    ):
        self.claim(
            "req_1",
            user_id="alice",
            global_active=1,
        )

        with self.assertRaises(
            ChatConcurrencyLimitExceeded
        ):
            self.claim(
                "req_2",
                user_id="bob",
                global_active=1,
            )

        self.finish("req_1")

        self.assertIsNone(
            self.claim(
                "req_2",
                user_id="bob",
                global_active=1,
            )
        )

    # 5. 滚动 24 小时窗口

    def test_rolling_24_hour_limit(self):
        self.claim("req_1", daily=1)
        self.finish("req_1")

        with self.assertRaises(
            ChatDailyLimitExceeded
        ):
            self.claim(
                "req_2",
                daily=1,
                now=self.NOW + timedelta(hours=23),
            )

        self.assertIsNone(
            self.claim(
                "req_2",
                daily=1,
                now=self.NOW + timedelta(hours=25),
            )
        )

    # 6. 不同用户独立计数

    def test_users_have_independent_daily_limits(self):
        self.claim(
            "req_1",
            user_id="alice",
            daily=1,
        )

        self.claim(
            "req_2",
            user_id="bob",
            daily=1,
        )

        with self.assertRaises(
            ChatDailyLimitExceeded
        ):
            self.claim(
                "req_3",
                user_id="alice",
                daily=1,
            )

    # 7. 请求归属与消息内容检查

    def test_other_user_and_changed_message_rejected_before_limits(
        self,
    ):
        self.claim("req_1", daily=1)

        with self.assertRaises(
            RequestOwnerMismatch
        ):
            self.claim(
                "req_1",
                user_id="bob",
                digest="other",
                daily=1,
            )

        with self.assertRaises(
            RequestMessageConflict
        ):
            self.claim(
                "req_1",
                digest="other",
                daily=1,
            )

    # 8. 旧记录不能被新用户冒领

    def test_legacy_record_cannot_be_claimed(self):

        with closing(self.connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO chat_requests (
                    request_id,
                    message_hash,
                    status,
                    reply,
                    created_at,
                    updated_at
                )
                VALUES (
                    'old_req',
                    'old_hash',
                    'completed',
                    '旧回复',
                    't1',
                    't1'
                )
                """
            )

        with self.assertRaises(
            RequestOwnerMismatch
        ):
            self.claim("old_req")

        with closing(self.connect()) as conn:
            row = conn.execute(
                """
                SELECT reply, user_id
                FROM chat_requests
                WHERE request_id = 'old_req'
                """
            ).fetchone()

        self.assertEqual(
            row["reply"],
            "旧回复",
        )

        self.assertIsNone(
            row["user_id"]
        )

    # 9. 多线程并发不能突破限制

    def test_parallel_new_requests_cannot_exceed_concurrency(
        self,
    ):
        def attempt(n):
            try:
                self.claim(
                    f"req_{n}",
                    daily=10,
                    per_user=1,
                )
                return "admitted"

            except ChatConcurrencyLimitExceeded:
                return "blocked"

        with ThreadPoolExecutor(
            max_workers=6
        ) as pool:
            results = list(
                pool.map(attempt, range(6))
            )

        self.assertEqual(
            results.count("admitted"),
            1,
        )

        self.assertEqual(
            results.count("blocked"),
            5,
        )

    # 10. 不合法的身份、权限和额度拒绝执行

    def test_invalid_user_role_or_limit_fail_closed(
        self,
    ):
        with self.assertRaises(ValueError):
            self.claim(
                "req_1",
                user_id="",
            )

        with self.assertRaises(ValueError):
            self.claim(
                "req_1",
                role="unrecognized",
            )

        with self.assertRaises(ValueError):
            self.claim(
                "req_1",
                daily=0,
            )

        with closing(self.connect()) as conn:
            count = conn.execute(
                """
                SELECT COUNT(*)
                FROM chat_requests
                """
            ).fetchone()[0]

        self.assertEqual(count, 0)

    # 11. 索引重复初始化不会失败

    def test_prepare_index_is_repeatable(self):

        with closing(self.connect()) as conn, conn:
            prepare_chat_quota(conn)
            prepare_chat_quota(conn)

        with closing(self.connect()) as conn:
            found = conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'index'
                  AND name =
                    'idx_chat_quota_user_created'
                """
            ).fetchone()

        self.assertIsNotNone(found)


if __name__ == "__main__":
    unittest.main()