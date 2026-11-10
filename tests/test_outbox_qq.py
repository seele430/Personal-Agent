"""QQ 通知接入 notification_outbox 的测试（全 mock）。"""

import io
import sqlite3
import tempfile
import unittest

from contextlib import closing, redirect_stdout
from unittest.mock import patch

from tests.helper import build_temp_paths, isolated_paths
from src.core import outbox_worker
from src.qq.onebot import OneBotError


class OutboxQqCase(unittest.TestCase):
    def make_db(self, temp_dir, owner="auth-owner", attempts=0):
        db_path = build_temp_paths(temp_dir).tasks_db

        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("""
                CREATE TABLE notification_outbox (
                    id INTEGER PRIMARY KEY,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    owner_id TEXT,
                    sent_at TEXT
                )
            """)
            conn.execute(
                "INSERT INTO notification_outbox (id, message, status, attempts, owner_id) VALUES (1, 'hello', 'pending', ?, ?)",
                (attempts, owner),
            )

        return db_path

    def row(self, db_path):
        with closing(sqlite3.connect(db_path)) as conn:
            return conn.execute(
                "SELECT status, attempts, next_attempt_at, sent_at FROM notification_outbox WHERE id=1"
            ).fetchone()

    def test_qq_success_replace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = self.make_db(temp_dir)

            with isolated_paths(temp_dir), patch.object(outbox_worker, "init_task_db"), patch.object(
                outbox_worker, "channel_for_owner", return_value="qq"
            ), patch.object(outbox_worker, "send_qq_notification") as qq_send, patch.object(
                outbox_worker, "send_notification"
            ) as ntfy_send:
                result = outbox_worker.process_one_notification()

            status, attempts, next_attempt, sent_at = self.row(db_path)

            self.assertTrue(result)
            self.assertEqual(status, "sent")
            self.assertEqual(attempts, 1)
            self.assertIsNone(next_attempt)
            self.assertIsNotNone(sent_at)
            qq_send.assert_called_once()
            ntfy_send.assert_not_called()

    def test_in_app_no_network(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = self.make_db(temp_dir)

            with isolated_paths(temp_dir), patch.object(outbox_worker, "init_task_db"), patch.object(
                outbox_worker, "channel_for_owner", return_value="in_app"
            ), patch.object(outbox_worker, "send_qq_notification") as qq_send, patch.object(
                outbox_worker, "send_notification"
            ) as ntfy_send:
                result = outbox_worker.process_one_notification()

            status, attempts, next_attempt, sent_at = self.row(db_path)

            self.assertFalse(result)
            self.assertEqual(status, "in_app")
            self.assertEqual(attempts, 0)
            self.assertIsNone(next_attempt)
            self.assertIsNone(sent_at)
            qq_send.assert_not_called()
            ntfy_send.assert_not_called()

    def test_onebot_error_retries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = self.make_db(temp_dir)

            with isolated_paths(temp_dir), patch.object(outbox_worker, "init_task_db"), patch.object(
                outbox_worker, "channel_for_owner", return_value="qq"
            ), patch.object(
                outbox_worker, "send_qq_notification", side_effect=OneBotError("down")
            ):
                result = outbox_worker.process_one_notification()

            status, attempts, next_attempt, _ = self.row(db_path)

            self.assertFalse(result)
            self.assertEqual(status, "pending")
            self.assertEqual(attempts, 1)
            self.assertIsNotNone(next_attempt)

    def test_fifth_failure_is_failed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = self.make_db(temp_dir, attempts=4)

            with isolated_paths(temp_dir), patch.object(outbox_worker, "init_task_db"), patch.object(
                outbox_worker, "channel_for_owner", return_value="qq"
            ), patch.object(
                outbox_worker, "send_qq_notification", side_effect=OneBotError("down")
            ):
                outbox_worker.process_one_notification()

            status, attempts, next_attempt, _ = self.row(db_path)

            self.assertEqual(status, "failed")
            self.assertEqual(attempts, 5)
            self.assertIsNone(next_attempt)

    def test_value_error_is_permanent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = self.make_db(temp_dir)

            with isolated_paths(temp_dir), patch.object(outbox_worker, "init_task_db"), patch.object(
                outbox_worker, "channel_for_owner", return_value="qq"
            ), patch.object(
                outbox_worker, "send_qq_notification", side_effect=ValueError("bad config")
            ), patch.object(outbox_worker, "send_notification") as ntfy_send:
                outbox_worker.process_one_notification()

            status, attempts, next_attempt, _ = self.row(db_path)

            self.assertEqual(status, "failed")
            self.assertEqual(attempts, 1)
            self.assertIsNone(next_attempt)
            ntfy_send.assert_not_called()

    def test_role_change_after_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = self.make_db(temp_dir)

            with isolated_paths(temp_dir), patch.object(outbox_worker, "init_task_db"), patch.object(
                outbox_worker, "channel_for_owner", return_value="qq"
            ), patch.object(
                outbox_worker, "send_qq_notification", side_effect=OneBotError("down")
            ):
                outbox_worker.process_one_notification()

            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute("UPDATE notification_outbox SET next_attempt_at='2000-01-01' WHERE id=1")

            with isolated_paths(temp_dir), patch.object(outbox_worker, "init_task_db"), patch.object(
                outbox_worker, "channel_for_owner", return_value="in_app"
            ), patch.object(outbox_worker, "send_qq_notification") as qq_send:
                outbox_worker.process_one_notification()

            status, _, _, _ = self.row(db_path)

            self.assertEqual(status, "in_app")
            qq_send.assert_not_called()



    def test_logs_do_not_contain_body(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = build_temp_paths(temp_dir).tasks_db

            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute("""
                    CREATE TABLE notification_outbox (
                        id INTEGER PRIMARY KEY, message TEXT NOT NULL, status TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT,
                        owner_id TEXT, sent_at TEXT
                    )
                """)
                conn.execute(
                    "INSERT INTO notification_outbox (id, message, status, owner_id) VALUES (1, 'SECRET-BODY', 'pending', 'auth-owner')"
                )

            output = io.StringIO()

            with isolated_paths(temp_dir), patch.object(outbox_worker, "init_task_db"), redirect_stdout(output), patch.object(
                outbox_worker, "channel_for_owner", return_value="qq"
            ), patch.object(outbox_worker, "send_qq_notification"):
                outbox_worker.process_one_notification()

            self.assertNotIn("SECRET-BODY", output.getvalue())


if __name__ == "__main__":
    unittest.main()
