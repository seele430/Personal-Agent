import sqlite3
import tempfile
import unittest

from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import requests

from tests.helper import build_temp_paths, isolated_paths
from src.core import outbox_worker


class TestOutbox429(unittest.TestCase):

    def test_429_respects_retry_after(self):

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = build_temp_paths(temp_dir).tasks_db

            # 创建独立测试数据库
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
                    VALUES (1, 'test 429', 'pending', 0)
                """)

            # 模拟服务器返回 429
            response = requests.Response()
            response.status_code = 429
            response.headers["Retry-After"] = "600"

            error = requests.HTTPError(
                "429 Too Many Requests",
                response=response
            )

            start_time = datetime.now(timezone.utc)

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

                # 再执行一次，不应该提前重试
                second_result = (
                    outbox_worker.process_one_notification()
                )

                self.assertFalse(second_result)
                mock_send.assert_called_once()

            # 检查数据库
            with closing(sqlite3.connect(db_path)) as conn:
                row = conn.execute("""
                    SELECT status, attempts, next_attempt_at
                    FROM notification_outbox
                    WHERE id = 1
                """).fetchone()

            status, attempts, next_attempt_at = row

            self.assertEqual(status, "pending")
            self.assertEqual(attempts, 1)
            self.assertIsNotNone(next_attempt_at)

            retry_time = datetime.fromisoformat(next_attempt_at)

            # 应遵守 Retry-After: 600 秒
            self.assertGreaterEqual(
                retry_time,
                start_time + timedelta(seconds=590)
            )


if __name__ == "__main__":
    unittest.main()