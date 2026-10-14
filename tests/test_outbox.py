from contextlib import closing
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import requests

from tests.helper import build_temp_paths, isolated_paths
from src.core import outbox_worker


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


if __name__ == "__main__":
    unittest.main()