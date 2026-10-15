import sqlite3
import tempfile
import unittest

from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.helper import build_temp_paths, isolated_paths
from src.core import outbox_worker


class TestOutboxBackoff(unittest.TestCase):

    def test_pending_notification_waits_until_due(self):

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = build_temp_paths(temp_dir).tasks_db

            future_time = (
                datetime.now(timezone.utc)
                + timedelta(hours=1)
            ).isoformat()

            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute("""
                    CREATE TABLE notification_outbox (
                        id INTEGER PRIMARY KEY,
                        message TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL,
                        next_attempt_at TEXT,
                        owner_id TEXT
                    )
                """)

                conn.execute(
                    """
                    INSERT INTO notification_outbox
                    (id, message, attempts, status, next_attempt_at)
                    VALUES (1, 'future test', 0, 'pending', ?)
                    """,
                    (future_time,)
                )

            with (
                isolated_paths(temp_dir),
                patch.object(outbox_worker, "init_task_db"),
                patch.object(
                    outbox_worker, "send_notification"
                ) as mock_send,
            ):
                result = outbox_worker.process_one_notification()

                self.assertFalse(result)
                mock_send.assert_not_called()

            with closing(sqlite3.connect(db_path)) as conn:
                status, attempts = conn.execute(
                    """
                    SELECT status, attempts
                    FROM notification_outbox
                    WHERE id = 1
                    """
                ).fetchone()

            self.assertEqual(status, "pending")
            self.assertEqual(attempts, 0)


if __name__ == "__main__":
    unittest.main()