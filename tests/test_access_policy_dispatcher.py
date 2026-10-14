
import sqlite3
import tempfile
import unittest

from contextlib import closing
from datetime import datetime, timezone
from unittest.mock import patch

import requests

from tests.helper import isolate_paths
from src.core import monitor_registry
from src.core import monitor_dispatcher
from src.core import task_run_history


RSS_URL = "https://blog.python.org/rss.xml"
PAST = "2000-01-01T00:00:00+00:00"


class TestAccessPolicyDispatcher(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        temporary = isolate_paths(self, self.temp_dir)
        self.db_path = temporary.tasks_db

        self.task_id = monitor_registry.create_monitor_task(
            "test_owner",
            RSS_URL,
            30
        )

        monitor_dispatcher.init_dispatcher_db()
        self.make_due()

    def make_due(self):
        with closing(
            sqlite3.connect(self.db_path)
        ) as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE monitor_tasks
                    SET next_run_at = ?
                    WHERE id = ?
                    """,
                    (PAST, self.task_id)
                )

    def state(self):
        with closing(
            sqlite3.connect(self.db_path)
        ) as conn:
            conn.row_factory = sqlite3.Row

            row = conn.execute(
                """
                SELECT *
                FROM monitor_tasks
                WHERE id = ?
                """,
                (self.task_id,)
            ).fetchone()

            return dict(row)

    def history(self):
        return task_run_history.list_task_runs(
            self.task_id
        )

    def simulate_http(self, status, retry_after=None):
        response = requests.Response()
        response.status_code = status
        response.url = RSS_URL

        if retry_after is not None:
            response.headers["Retry-After"] = str(
                retry_after
            )

        error = requests.HTTPError(
            f"HTTP {status}",
            response=response
        )

        with patch.object(
            monitor_dispatcher,
            "run_rss_monitor",
            side_effect=error
        ):
            return (
                monitor_dispatcher.run_due_monitor_tasks()
            )

    def test_403_pauses_task(self):
        self.assertEqual(
            self.simulate_http(403),
            1
        )

        state = self.state()

        self.assertEqual(state["status"], "paused")
        self.assertIsNone(state["next_run_at"])
        self.assertIn("403", state["last_error"])
        self.assertIsNone(state["lease_token"])
        self.assertEqual(
            self.history()[0]["status"],
            "failed"
        )

    def test_429_respects_retry_after(self):
        self.assertEqual(
            self.simulate_http(429, 600),
            1
        )

        state = self.state()

        self.assertEqual(state["status"], "active")
        self.assertEqual(state["failure_count"], 1)
        self.assertIn("429", state["last_error"])

        delay = (
            datetime.fromisoformat(
                state["next_run_at"]
            ) - datetime.now(timezone.utc)
        ).total_seconds()

        self.assertGreater(delay, 580)
        self.assertLess(delay, 605)

        self.assertEqual(
            self.history()[0]["status"],
            "failed"
        )

    def test_503_is_delayed(self):
        self.assertEqual(
            self.simulate_http(503),
            1
        )

        state = self.state()

        self.assertEqual(state["status"], "active")
        self.assertEqual(state["failure_count"], 1)
        self.assertIn("503", state["last_error"])

        delay = (
            datetime.fromisoformat(
                state["next_run_at"]
            ) - datetime.now(timezone.utc)
        ).total_seconds()

        self.assertGreater(delay, 45)
        self.assertLess(delay, 65)

    def test_repeated_failures_backoff(self):
        self.simulate_http(503)

        first = self.state()
        self.assertEqual(first["failure_count"], 1)

        self.make_due()
        self.simulate_http(503)

        second = self.state()

        self.assertEqual(second["failure_count"], 2)
        self.assertEqual(second["status"], "active")

        delay = (
            datetime.fromisoformat(
                second["next_run_at"]
            ) - datetime.now(timezone.utc)
        ).total_seconds()

        self.assertGreater(delay, 105)
        self.assertLess(delay, 125)

    def test_timeout_is_recorded(self):
        with patch.object(
            monitor_dispatcher,
            "run_rss_monitor",
            side_effect=TimeoutError(
                "Simulated RSS timeout"
            )
        ):
            result = (
                monitor_dispatcher.run_due_monitor_tasks()
            )

        self.assertEqual(result, 1)

        state = self.state()

        self.assertEqual(state["status"], "active")
        self.assertEqual(state["failure_count"], 1)
        self.assertIn(
            "TimeoutError",
            state["last_error"]
        )

        self.assertEqual(
            self.history()[0]["status"],
            "failed"
        )

    def test_success_resets_failure_count(self):
        with closing(
            sqlite3.connect(self.db_path)
        ) as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE monitor_tasks
                    SET failure_count = 2
                    WHERE id = ?
                    """,
                    (self.task_id,)
                )

        with patch.object(
            monitor_dispatcher,
            "run_rss_monitor",
            return_value=0
        ):
            result = (
                monitor_dispatcher.run_due_monitor_tasks()
            )

        self.assertEqual(result, 1)

        state = self.state()

        self.assertEqual(state["failure_count"], 0)
        self.assertIsNone(state["last_error"])

        self.assertEqual(
            self.history()[0]["status"],
            "success"
        )


if __name__ == "__main__":
    unittest.main()