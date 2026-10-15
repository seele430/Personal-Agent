from contextlib import closing
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import requests

from tests.helper import build_temp_paths, isolated_paths
from src.core import outbox_worker
from src.core.task_engine import detect_new_items, init_task_db


class TestOutboxRetry(unittest.TestCase):

    def test_network_failure_keeps_notification_pending(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = build_temp_paths(temp_dir).tasks_db

            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute("""
                    CREATE TABLE notification_outbox (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        monitor_id TEXT NOT NULL,
                        item_id TEXT NOT NULL,
                        message TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at TEXT,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                        sent_at TEXT,
                        UNIQUE (monitor_id, item_id)
                    )
                """)

                conn.execute("""
                    INSERT INTO notification_outbox
                    (monitor_id, item_id, message)
                    VALUES ('test', 'article-1', 'test notification')
                """)

            with (
                isolated_paths(temp_dir),
                patch.object(outbox_worker, "init_task_db"),
                patch.object(
                    outbox_worker,
                    "send_notification",
                    side_effect=requests.ConnectionError(
                        "Simulated network failure"
                    ),
                ),
            ):
                result = outbox_worker.process_one_notification()

            with closing(sqlite3.connect(db_path)) as conn, conn:
                status, attempts, next_attempt_at = conn.execute(
                    """
                    SELECT status, attempts, next_attempt_at
                    FROM notification_outbox
                    WHERE item_id = 'article-1'
                    """
                ).fetchone()

            self.assertFalse(result)
            self.assertEqual(status, "pending")
            self.assertEqual(attempts, 1)
            self.assertIsNotNone(next_attempt_at)


class TestOutboxOwnership(unittest.TestCase):
    """外发通知必须带得走"这条通知是谁的"。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        self.db_path = build_temp_paths(
            self.temp
        ).tasks_db

        isolated = isolated_paths(self.temp)
        isolated.__enter__()
        self.addCleanup(
            isolated.__exit__, None, None, None
        )

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def columns(self, table):
        with closing(self.connect()) as conn:
            return {
                row[1]
                for row in conn.execute(
                    f"PRAGMA table_info({table})"
                )
            }

    def outbox_rows(self):
        with closing(self.connect()) as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM notification_outbox"
                    " ORDER BY id"
                )
            ]

    def create_monitor_task(
        self,
        task_id,
        owner_id,
    ):
        with closing(self.connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS monitor_tasks (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL
                )
                """
            )

            conn.execute(
                """
                INSERT OR REPLACE INTO monitor_tasks
                (id, owner_id)
                VALUES (?, ?)
                """,
                (task_id, owner_id),
            )

    @staticmethod
    def article(item_id, title="新文章"):
        return {
            "id": item_id,
            "title": title,
            "link": f"https://example.com/{item_id}",
        }

    # 1. 旧表通过非破坏性迁移补上归属列，旧记录保持未归属

    def test_legacy_table_gains_owner_column(self):
        with closing(self.connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE notification_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    monitor_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    sent_at TEXT,
                    UNIQUE (monitor_id, item_id)
                )
                """
            )

            conn.execute(
                """
                INSERT INTO notification_outbox
                (monitor_id, item_id, message)
                VALUES ('m1', 'i1', '迁移前的通知')
                """
            )

        self.assertNotIn(
            "owner_id",
            self.columns("notification_outbox"),
        )

        init_task_db()

        self.assertIn(
            "owner_id",
            self.columns("notification_outbox"),
        )

        rows = self.outbox_rows()

        self.assertEqual(len(rows), 1)

        # 不把历史数据自动分配给任何人。
        self.assertIsNone(rows[0]["owner_id"])

    # 2. 新通知带上任务主人的归属

    def test_new_notification_carries_task_owner(self):
        init_task_db()

        self.create_monitor_task(
            "rss_001",
            "owner_001",
        )

        # 第一次只建立基线，不产生通知
        detect_new_items(
            "rss_001",
            [self.article("a1")],
        )

        self.assertEqual(self.outbox_rows(), [])

        # 第二次出现新文章
        detect_new_items(
            "rss_001",
            [
                self.article("a1"),
                self.article("a2"),
            ],
        )

        rows = self.outbox_rows()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["owner_id"], "owner_001")
        self.assertEqual(rows[0]["monitor_id"], "rss_001")

    # 3. 查不到任务时不猜归属

    def test_unknown_task_keeps_owner_empty(self):
        init_task_db()

        self.create_monitor_task(
            "rss_001",
            "owner_001",
        )

        detect_new_items(
            "rss_999",
            [self.article("b1")],
        )

        detect_new_items(
            "rss_999",
            [
                self.article("b1"),
                self.article("b2"),
            ],
        )

        rows = self.outbox_rows()

        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["owner_id"])

    # 4. 没有 monitor_tasks 表时（独立使用任务引擎）也不能报错

    def test_engine_works_without_monitor_tasks(self):
        detect_new_items(
            "rss_777",
            [self.article("c1")],
        )

        detect_new_items(
            "rss_777",
            [
                self.article("c1"),
                self.article("c2"),
            ],
        )

        rows = self.outbox_rows()

        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["owner_id"])

    # 5. 两个任务的通知不会串到对方名下

    def test_owners_do_not_cross(self):
        init_task_db()

        self.create_monitor_task("rss_a", "owner_a")
        self.create_monitor_task("rss_b", "owner_b")

        for task_id in ("rss_a", "rss_b"):
            detect_new_items(
                task_id,
                [self.article("x1")],
            )

            detect_new_items(
                task_id,
                [
                    self.article("x1"),
                    self.article("x2"),
                ],
            )

        owners = {
            row["monitor_id"]: row["owner_id"]
            for row in self.outbox_rows()
        }

        self.assertEqual(
            owners,
            {
                "rss_a": "owner_a",
                "rss_b": "owner_b",
            },
        )


if __name__ == "__main__":
    unittest.main()