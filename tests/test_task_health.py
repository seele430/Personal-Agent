
import sqlite3
import tempfile
import unittest

from contextlib import closing
from datetime import datetime, timedelta, timezone

from tests.helper import isolate_paths
from src.core import monitor_registry
from src.core import monitor_dispatcher
from src.core import task_run_history
from src.core import task_health


RSS_URL = "https://blog.python.org/rss.xml"
PAST = "2000-01-01T00:00:00+00:00"


class TestTaskHealth(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        temporary = isolate_paths(self, self.temp_dir)
        self.db_path = temporary.tasks_db

        monitor_dispatcher.init_dispatcher_db()

        self.task_id = monitor_registry.create_monitor_task(
            "test_owner",
            RSS_URL,
            30
        )

    def update_task(self, sql, parameters):
        with closing(
            sqlite3.connect(self.db_path)
        ) as conn:
            with conn:
                conn.execute(sql, parameters)

    def test_healthy_task_has_no_findings(self):
        """正常任务不应该产生告警。"""

        issues = task_health.scan_task_health()

        self.assertEqual(issues, [])

    def test_repeated_failures_detected(self):
        """连续失败达到阈值时报告异常。"""

        self.update_task(
            """
            UPDATE monitor_tasks
            SET failure_count = 3,
                last_error = 'Simulated timeout'
            WHERE id = ?
            """,
            (self.task_id,)
        )

        issues = task_health.scan_task_health()

        self.assertEqual(len(issues), 1)

        self.assertEqual(
            issues[0]["type"],
            "repeated_failures"
        )

        self.assertEqual(
            issues[0]["failure_count"],
            3
        )

    def test_paused_error_detected(self):
        """权限错误导致暂停应报告给管理员。"""

        self.update_task(
            """
            UPDATE monitor_tasks
            SET status = 'paused',
                last_error = 'HTTP 403 Forbidden'
            WHERE id = ?
            """,
            (self.task_id,)
        )

        issues = task_health.scan_task_health()

        self.assertEqual(len(issues), 1)

        self.assertEqual(
            issues[0]["type"],
            "paused_error"
        )

    def test_expired_run_detected_without_changes(self):
        """过期租约被发现，但不修改原始记录。"""

        run_id = task_run_history.start_task_run(
            self.task_id,
            "old-token"
        )

        self.update_task(
            """
            UPDATE task_run_history
            SET started_at = ?
            WHERE run_id = ?
            """,
            (PAST, run_id)
        )

        self.update_task(
            """
            UPDATE monitor_tasks
            SET lease_token = ?,
                lease_expires_at = ?
            WHERE id = ?
            """,
            ("old-token", PAST, self.task_id)
        )

        issues = task_health.scan_task_health()

        stuck = [
            issue for issue in issues
            if issue["type"] == "suspected_stuck_run"
        ]

        self.assertEqual(len(stuck), 1)

        self.assertEqual(
            stuck[0]["reason"],
            "lease_expired"
        )

        with closing(
            sqlite3.connect(self.db_path)
        ) as conn:
            status = conn.execute(
                """
                SELECT status
                FROM task_run_history
                WHERE run_id = ?
                """,
                (run_id,)
            ).fetchone()[0]

        # 健康检查不能擅自修改运行记录
        self.assertEqual(status, "running")

    def test_recent_run_not_flagged(self):
        """新启动、租约有效的任务不应被误报。"""

        task_run_history.start_task_run(
            self.task_id,
            "current-token"
        )

        future = (
            datetime.now(timezone.utc)
            + timedelta(minutes=5)
        ).isoformat()

        self.update_task(
            """
            UPDATE monitor_tasks
            SET lease_token = ?,
                lease_expires_at = ?
            WHERE id = ?
            """,
            ("current-token", future, self.task_id)
        )

        issues = task_health.scan_task_health()

        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()