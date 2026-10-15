import sqlite3
import tempfile
import unittest

from contextlib import closing
from unittest.mock import patch

import requests

from tests.helper import build_temp_paths, isolated_paths
from src.core import outbox_worker


class TestOutboxHTTP(unittest.TestCase):

    def test_403_stops_retry(self):

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = build_temp_paths(temp_dir).tasks_db

            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute("""
                    CREATE TABLE notification_outbox (
                        id INTEGER PRIMARY KEY,
                        message TEXT NOT NULL,
                        status TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at TEXT,
                        owner_id TEXT
                    )
                """)

                conn.execute("""
                    INSERT INTO notification_outbox
                    (id, message, status, attempts)
                    VALUES (1, 'test 403', 'pending', 0)
                """)

            # 模拟服务器返回 HTTP 403
            response = requests.Response()
            response.status_code = 403

            error = requests.HTTPError(
                "403 Forbidden",
                response=response
            )

            with (
                isolated_paths(temp_dir),
                patch.object(outbox_worker, "init_task_db"),
                patch.object(
                    outbox_worker,
                    "send_notification",
                    side_effect=error
                ) as mock_send,
            ):
                result = outbox_worker.process_one_notification()

                self.assertFalse(result)
                mock_send.assert_called_once()

            with closing(sqlite3.connect(db_path)) as conn:
                row = conn.execute("""
                    SELECT status, attempts, next_attempt_at
                    FROM notification_outbox
                    WHERE id = 1
                """).fetchone()

            status, attempts, next_attempt_at = row

            self.assertEqual(status, "failed")
            self.assertEqual(attempts, 1)
            self.assertIsNone(next_attempt_at)


if __name__ == "__main__":
    unittest.main()