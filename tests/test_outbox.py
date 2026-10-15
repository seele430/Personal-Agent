from contextlib import closing
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import requests

from tests.helper import build_temp_paths, isolated_paths
from src.core import outbox_worker
from src.core.notification_delivery import (
    IN_APP,
    NTFY,
    channel_for_owner,
    channel_for_role,
    role_of_user,
)
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
                        owner_id TEXT,
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


class TestDeliveryChannel(unittest.TestCase):
    """按人投递：只有站长的通知才进全局 ntfy。

    这是本轮要锁死的安全属性：
    管理员/普通用户的任务结果**不能**推到站长手机上。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        temporary = build_temp_paths(self.temp)
        self.db_path = temporary.tasks_db
        self.auth_path = temporary.auth_users_db

        isolated = isolated_paths(self.temp)
        isolated.__enter__()
        self.addCleanup(
            isolated.__exit__, None, None, None
        )

        self.create_user("owner_001", "owner")
        self.create_user("admin_001", "admin")

    def create_user(self, user_id, role):
        with closing(
            sqlite3.connect(self.auth_path)
        ) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    role TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                )
                """
            )

            conn.execute(
                """
                INSERT OR REPLACE INTO auth_users
                (id, username, role, active)
                VALUES (?, ?, ?, 1)
                """,
                (user_id, user_id, role),
            )

    def queue(self, owner_id, message="新内容"):
        init_task_db()

        with closing(
            sqlite3.connect(self.db_path)
        ) as conn, conn:
            conn.execute(
                """
                INSERT INTO notification_outbox
                (monitor_id, item_id, message, owner_id)
                VALUES ('m1', ?, ?, ?)
                """,
                (f"item-{message}", message, owner_id),
            )

    def status_of(self, message):
        with closing(
            sqlite3.connect(self.db_path)
        ) as conn:
            return conn.execute(
                """
                SELECT status FROM notification_outbox
                WHERE message = ?
                """,
                (message,),
            ).fetchone()[0]

    # ---------- 策略本身 ----------

    def test_channel_for_role(self):
        self.assertEqual(channel_for_role("owner"), NTFY)

        for role in ("admin", "trusted", "member", None):
            self.assertEqual(channel_for_role(role), IN_APP)

    def test_role_lookup(self):
        self.assertEqual(
            role_of_user("owner_001"), "owner"
        )
        self.assertEqual(
            role_of_user("admin_001"), "admin"
        )
        # 查不到就不猜
        self.assertIsNone(role_of_user("ghost"))
        self.assertIsNone(role_of_user(None))
        self.assertIsNone(role_of_user(""))

    def test_channel_for_owner(self):
        self.assertEqual(
            channel_for_owner("owner_001"), NTFY
        )
        self.assertEqual(
            channel_for_owner("admin_001"), IN_APP
        )
        # 主人查不到 → 最保守：站内，不误推别人手机
        self.assertEqual(channel_for_owner("ghost"), IN_APP)
        # 迁移前没有归属的历史行沿用旧的全局推送
        self.assertEqual(channel_for_owner(None), NTFY)

    # ---------- worker 行为 ----------

    def test_owner_notification_is_sent(self):
        self.queue("owner_001")

        with patch.object(
            outbox_worker, "send_notification"
        ) as sender:
            result = outbox_worker.process_one_notification()

        self.assertTrue(result)
        sender.assert_called_once()
        self.assertEqual(self.status_of("新内容"), "sent")

    def test_admin_notification_is_diverted_to_in_app(self):
        self.queue("admin_001")

        with patch.object(
            outbox_worker, "send_notification"
        ) as sender:
            result = outbox_worker.process_one_notification()

        # 关键断言：没有发出任何网络请求
        sender.assert_not_called()
        self.assertFalse(result)
        self.assertEqual(
            self.status_of("新内容"), "in_app"
        )

    def test_unknown_owner_is_diverted(self):
        self.queue("ghost")

        with patch.object(
            outbox_worker, "send_notification"
        ) as sender:
            outbox_worker.process_one_notification()

        sender.assert_not_called()
        self.assertEqual(
            self.status_of("新内容"), "in_app"
        )

    def test_legacy_row_without_owner_still_sends(self):
        self.queue(None)

        with patch.object(
            outbox_worker, "send_notification"
        ) as sender:
            outbox_worker.process_one_notification()

        sender.assert_called_once()
        self.assertEqual(self.status_of("新内容"), "sent")


if __name__ == "__main__":
    unittest.main()