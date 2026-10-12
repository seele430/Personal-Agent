"""测试聊天请求归属：只使用临时数据库，不接触真实数据。"""

import sqlite3
import unittest

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from src.core.chat_ownership import (
    RequestMessageConflict,
    RequestOwnerMismatch,
    claim_owned_request,
    migrate_chat_ownership,
    read_owned_record,
    update_owned_status,
)


class TestChatOwnership(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "requests_test.db"

        with closing(self.connect()) as conn, conn:
            # 完全模拟现有 api.py 的旧表结构。
            conn.execute(
                """
                CREATE TABLE chat_requests (
                    request_id TEXT PRIMARY KEY,
                    message_hash TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN (
                            'processing', 'completed', 'unknown'
                        )),
                    reply TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def tearDown(self):
        self.temp.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def migrate(self):
        with closing(self.connect()) as conn, conn:
            migrate_chat_ownership(conn)

    def insert_legacy(self, request_id="legacy_12345"):
        with closing(self.connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO chat_requests
                (request_id, message_hash, status, reply, created_at, updated_at)
                VALUES (?, 'old_hash', 'completed', '原有回复', 't1', 't1')
                """,
                (request_id,),
            )

    def test_non_destructive_migration_preserves_legacy_data(self):
        self.insert_legacy()
        self.migrate()

        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM chat_requests WHERE request_id = ?",
                ("legacy_12345",),
            ).fetchone()

            self.assertIsNotNone(row)
            self.assertEqual(row["reply"], "原有回复")
            self.assertEqual(row["message_hash"], "old_hash")
            self.assertIsNone(row["user_id"])

    def test_migration_is_safe_to_repeat(self):
        self.migrate()
        self.migrate()

        with closing(self.connect()) as conn:
            columns = [
                row["name"]
                for row in conn.execute("PRAGMA table_info(chat_requests)")
            ]
            self.assertEqual(columns.count("user_id"), 1)

    def test_missing_original_table_fails_without_creating_it(self):
        with closing(sqlite3.connect(":memory:")) as conn:
            with self.assertRaises(sqlite3.OperationalError):
                migrate_chat_ownership(conn)

    def test_new_claim_is_owned_and_duplicate_returns_cache(self):
        self.migrate()

        with closing(self.connect()) as conn:
            first = claim_owned_request(
                conn, "request_123", "hash1", "alice", "t1"
            )
        with closing(self.connect()) as conn:
            repeated = claim_owned_request(
                conn, "request_123", "hash1", "alice", "t2"
            )

        self.assertIsNone(first)
        self.assertEqual(repeated["status"], "processing")

        with closing(self.connect()) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM chat_requests"
            ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_same_owner_changed_message_is_rejected(self):
        self.migrate()
        with closing(self.connect()) as conn:
            claim_owned_request(
                conn, "request_456", "hash1", "alice", "t1"
            )

        with closing(self.connect()) as conn:
            with self.assertRaises(RequestMessageConflict):
                claim_owned_request(
                    conn, "request_456", "hash2", "alice", "t2"
                )

    def test_other_user_cannot_reuse_existing_request_id(self):
        self.migrate()
        with closing(self.connect()) as conn:
            claim_owned_request(
                conn, "request_789", "hash1", "alice", "t1"
            )

        with closing(self.connect()) as conn:
            with self.assertRaises(RequestOwnerMismatch):
                claim_owned_request(
                    conn, "request_789", "hash1", "bob", "t2"
                )

    def test_old_request_cannot_be_claimed_by_any_new_user(self):
        self.insert_legacy()
        self.migrate()
        with closing(self.connect()) as conn:
            self.assertIsNone(
                read_owned_record(conn, "legacy_12345", "alice")
            )
            with self.assertRaises(RequestOwnerMismatch):
                claim_owned_request(
                    conn, "legacy_12345", "old_hash", "alice", "t2"
                )

    def test_status_read_and_update_require_owner(self):
        self.migrate()
        with closing(self.connect()) as conn:
            claim_owned_request(
                conn, "request_abc", "hash1", "alice", "t1"
            )

        with closing(self.connect()) as conn:
            self.assertIsNone(
                read_owned_record(conn, "request_abc", "bob")
            )
            self.assertFalse(
                update_owned_status(
                    conn, "request_abc", "bob", "completed", "窃取", "t2"
                )
            )
            self.assertTrue(
                update_owned_status(
                    conn, "request_abc", "alice", "completed", "你好", "t3"
                )
            )
            self.assertFalse(
                update_owned_status(
                    conn, "request_abc", "alice", "completed", "重复", "t4"
                )
            )
            self.assertEqual(
                read_owned_record(conn, "request_abc", "alice")["reply"],
                "你好",
            )

    def test_different_users_can_have_independent_requests(self):
        self.migrate()
        with closing(self.connect()) as conn:
            claim_owned_request(conn, "alice_123", "a", "alice", "t1")
            claim_owned_request(conn, "bob_12345", "b", "bob", "t1")

        with closing(self.connect()) as conn:
            self.assertIsNotNone(
                read_owned_record(conn, "alice_123", "alice")
            )
            self.assertIsNone(
                read_owned_record(conn, "alice_123", "bob")
            )
            self.assertIsNotNone(
                read_owned_record(conn, "bob_12345", "bob")
            )

    def test_concurrent_duplicates_create_one_record(self):
        self.migrate()

        def claim_once(_):
            with closing(self.connect()) as conn:
                return claim_owned_request(
                    conn, "concurrent_123", "hash1", "alice", "t1"
                )

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(claim_once, range(8)))

        self.assertEqual(
            sum(result is None for result in results), 1
        )
        self.assertEqual(
            sum(result is not None for result in results), 7
        )

        with closing(self.connect()) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM chat_requests WHERE request_id = ?",
                ("concurrent_123",),
            ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_empty_user_id_is_rejected(self):
        self.migrate()
        with closing(self.connect()) as conn:
            with self.assertRaises(ValueError):
                claim_owned_request(
                    conn, "new_request", "hash", "", "t1"
                )
            with self.assertRaises(ValueError):
                read_owned_record(conn, "new_request", "")
            with self.assertRaises(ValueError):
                update_owned_status(
                    conn, "new_request", "", "completed", "x", "t2"
                )


if __name__ == "__main__":
    unittest.main()
