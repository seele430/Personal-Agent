
import sqlite3
import tempfile
import unittest

from contextlib import closing, ExitStack
from pathlib import Path
from unittest.mock import patch

from src.core import monitor_registry
from src.core import monitor_dispatcher
from src.core import task_run_history


RSS_URL = "https://blog.python.org/rss.xml"
PAST_TIME = "2000-01-01T00:00:00+00:00"


class TestDispatcherHistory(unittest.TestCase):

    def setUp(self):
        """使用独立临时数据库，隔离真实任务。"""

        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        self.db_path = (
            Path(self.temp_dir.name) / "test_tasks.db"
        )

        self.patches = ExitStack()
        self.addCleanup(self.patches.close)

        for module in (
            monitor_registry,
            monitor_dispatcher,
            task_run_history
        ):
            self.patches.enter_context(
                patch.object(module, "DB_PATH", self.db_path)
            )

        self.task_id = monitor_registry.create_monitor_task(
            "test_owner",
            RSS_URL,
            30
        )

        # 让测试任务立即到期
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE monitor_tasks
                    SET next_run_at = ?
                    WHERE id = ?
                    """,
                    (PAST_TIME, self.task_id)
                )

    def test_success_creates_history(self):
        """RSS 成功后应生成一条 success 历史。"""

        with patch.object(
            monitor_dispatcher,
            "run_rss_monitor",
            return_value=2
        ) as mock_rss:

            count = (
                monitor_dispatcher.run_due_monitor_tasks()
            )

        self.assertEqual(count, 1)

        mock_rss.assert_called_once_with(
            monitor_id=self.task_id,
            feed_url=RSS_URL
        )

        records = task_run_history.list_task_runs(
            self.task_id
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "success")
        self.assertEqual(records[0]["new_items"], 2)
        self.assertIsNotNone(records[0]["finished_at"])
        self.assertGreaterEqual(
            records[0]["duration_ms"], 0
        )

        # 刚执行过的任务不能立即重复执行
        self.assertEqual(
            monitor_dispatcher.run_due_monitor_tasks(),
            0
        )

    def test_failure_creates_history(self):
        """RSS 抛出异常时应生成 failed 历史。"""

        with patch.object(
            monitor_dispatcher,
            "run_rss_monitor",
            side_effect=TimeoutError(
                "Simulated RSS timeout"
            )
        ):

            count = (
                monitor_dispatcher.run_due_monitor_tasks()
            )

        self.assertEqual(count, 1)

        records = task_run_history.list_task_runs(
            self.task_id
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "failed")

        self.assertIn(
            "Simulated RSS timeout",
            records[0]["error_message"]
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                """
                SELECT
                    status,
                    last_error,
                    lease_token,
                    lease_expires_at
                FROM monitor_tasks
                WHERE id = ?
                """,
                (self.task_id,)
            ).fetchone()

        self.assertEqual(row[0], "active")
        self.assertIn("TimeoutError", row[1])
        self.assertIsNone(row[2])
        self.assertIsNone(row[3])


if __name__ == "__main__":
    unittest.main()