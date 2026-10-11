
import sqlite3
import tempfile
import unittest

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src.core import admin_alerts


BASE_TIME = datetime(
    2026, 1, 1, tzinfo=timezone.utc
)


def make_issue(task_id="rss_test"):
    return {
        "type": "repeated_failures",
        "task_id": task_id,
        "owner_id": "test_owner",
        "failure_count": 3,
        "last_error": "Simulated timeout",
    }


class TestAdminAlerts(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        self.db_path = (
            Path(self.temp_dir.name) / "alerts.db"
        )

        db_patch = patch.object(
            admin_alerts, "DB_PATH", self.db_path
        )

        db_patch.start()
        self.addCleanup(db_patch.stop)

    def collect(self, issues, current_time=BASE_TIME):
        with (
            patch.object(
                admin_alerts,
                "scan_task_health",
                return_value=issues
            ),
            patch.object(
                admin_alerts,
                "utc_now",
                return_value=current_time
            ),
        ):
            return admin_alerts.enqueue_health_alerts()

    def test_first_detection_creates_alert(self):
        """第一次异常生成一条待处理告警。"""

        result = self.collect([make_issue()])

        self.assertEqual(result, 1)

        alerts = admin_alerts.list_pending_admin_alerts()

        self.assertEqual(len(alerts), 1)
        self.assertEqual(
            alerts[0]["status"], "pending"
        )

    def test_repeated_scan_does_not_duplicate(self):
        """同一个故障重复扫描不重复入队。"""

        self.assertEqual(
            self.collect([make_issue()]), 1
        )

        self.assertEqual(
            self.collect([make_issue()]), 0
        )

        self.assertEqual(
            len(admin_alerts.list_pending_admin_alerts()),
            1
        )

    def test_pending_prevents_duplicate_after_cooldown(self):
        """即使冷却期结束，pending 也不重复入队。"""

        self.collect([make_issue()])

        later = BASE_TIME + timedelta(hours=2)

        result = self.collect(
            [make_issue()],
            current_time=later
        )

        self.assertEqual(result, 0)

        self.assertEqual(
            len(admin_alerts.list_pending_admin_alerts()),
            1
        )

    def test_cooldown_after_sent(self):
        """已处理的告警在冷却结束后可以再次提醒。"""

        self.collect([make_issue()])

        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE admin_alert_outbox
                    SET status = 'sent'
                    WHERE status = 'pending'
                    """
                )

        # 30 分钟时仍处于冷却期
        early = BASE_TIME + timedelta(minutes=30)

        self.assertEqual(
            self.collect([make_issue()], early),
            0
        )

        # 61 分钟后允许重新入队
        later = BASE_TIME + timedelta(minutes=61)

        self.assertEqual(
            self.collect([make_issue()], later),
            1
        )

        self.assertEqual(
            len(admin_alerts.list_pending_admin_alerts()),
            1
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            total = conn.execute(
                """
                SELECT COUNT(*)
                FROM admin_alert_outbox
                """
            ).fetchone()[0]

        self.assertEqual(total, 2)

    def test_alert_does_not_expose_error_details(self):
        """管理员推送摘要不包含原始敏感异常。"""

        issue = make_issue()

        issue["last_error"] = (
            "Secret API token: sk-test-do-not-share"
        )

        self.collect([issue])

        alerts = admin_alerts.list_pending_admin_alerts()

        self.assertEqual(len(alerts), 1)

        message = alerts[0]["message"]

        self.assertIn("管理员告警", message)
        self.assertNotIn("sk-test", message)
        self.assertNotIn("Secret API token", message)

    def test_different_tasks_create_separate_alerts(self):
        """不同监控任务分别生成告警。"""

        result = self.collect([
            make_issue("rss_one"),
            make_issue("rss_two"),
        ])

        self.assertEqual(result, 2)

        self.assertEqual(
            len(admin_alerts.list_pending_admin_alerts()),
            2
        )


if __name__ == "__main__":
    unittest.main()