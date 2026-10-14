
import sqlite3
import tempfile
import unittest

from contextlib import closing

from tests.helper import isolated_paths
from src.core import monitor_registry
from src.core import monitor_dispatcher


RSS_URL = "https://blog.python.org/rss.xml"
PAST_TIME = "2000-01-01T00:00:00+00:00"


class TestMonitorLease(unittest.TestCase):

    def test_expired_lease_can_be_reclaimed(self):
        """模拟 Worker 崩溃与租约重新领取。"""

        with tempfile.TemporaryDirectory() as temp_dir:
            with isolated_paths(temp_dir) as temporary:
                db_path = temporary.tasks_db

                task_id = monitor_registry.create_monitor_task(
                    "test_owner",
                    RSS_URL,
                    30
                )

                # 将任务设置为已到期
                with closing(sqlite3.connect(db_path)) as conn:
                    with conn:
                        conn.execute(
                            """
                            UPDATE monitor_tasks
                            SET next_run_at = ?
                            WHERE id = ?
                            """,
                            (PAST_TIME, task_id)
                        )

                # Worker A 领取
                first = monitor_dispatcher.claim_due_tasks()

                self.assertEqual(len(first), 1)
                self.assertEqual(first[0]["id"], task_id)

                token_a = first[0]["lease_token"]

                # 租约未过期，不能重复领取
                self.assertEqual(
                    monitor_dispatcher.claim_due_tasks(),
                    []
                )

                # 模拟 Worker A 崩溃，租约过期
                with closing(sqlite3.connect(db_path)) as conn:
                    with conn:
                        conn.execute(
                            """
                            UPDATE monitor_tasks
                            SET lease_expires_at = ?
                            WHERE id = ?
                            """,
                            (PAST_TIME, task_id)
                        )

                # Worker B 重新领取
                second = monitor_dispatcher.claim_due_tasks()

                self.assertEqual(len(second), 1)

                token_b = second[0]["lease_token"]

                self.assertNotEqual(token_a, token_b)

                # 旧 Worker 不能覆盖新 Worker 状态
                self.assertFalse(
                    monitor_dispatcher.complete_task(first[0])
                )

                # 新 Worker 可以完成任务
                self.assertTrue(
                    monitor_dispatcher.complete_task(second[0])
                )

                with closing(sqlite3.connect(db_path)) as conn:
                    row = conn.execute(
                        """
                        SELECT
                            lease_token,
                            lease_expires_at,
                            last_checked_at,
                            next_run_at
                        FROM monitor_tasks
                        WHERE id = ?
                        """,
                        (task_id,)
                    ).fetchone()

                self.assertIsNone(row[0])
                self.assertIsNone(row[1])
                self.assertIsNotNone(row[2])
                self.assertIsNotNone(row[3])

    def test_paused_and_archived_tasks_not_claimed(self):
        """暂停与归档的任务不能被领取。"""

        with tempfile.TemporaryDirectory() as temp_dir:
            with isolated_paths(temp_dir) as temporary:
                db_path = temporary.tasks_db

                paused_id = monitor_registry.create_monitor_task(
                    "test_owner", RSS_URL, 30
                )

                archived_id = monitor_registry.create_monitor_task(
                    "test_owner", RSS_URL, 30
                )

                with closing(sqlite3.connect(db_path)) as conn:
                    with conn:
                        conn.execute(
                            """
                            UPDATE monitor_tasks
                            SET status = 'paused',
                                next_run_at = ?
                            WHERE id = ?
                            """,
                            (PAST_TIME, paused_id)
                        )

                        conn.execute(
                            """
                            UPDATE monitor_tasks
                            SET archived_at = ?,
                                next_run_at = ?
                            WHERE id = ?
                            """,
                            (PAST_TIME, PAST_TIME, archived_id)
                        )

                claimed = monitor_dispatcher.claim_due_tasks()

                self.assertEqual(claimed, [])


if __name__ == "__main__":
    unittest.main()