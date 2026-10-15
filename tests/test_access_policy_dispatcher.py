
import os
import sqlite3
import tempfile
import unittest

from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import requests

from tests.helper import isolate_paths
from src.core import monitor_registry
from src.core import monitor_dispatcher
from src.core import source_registry
from src.core import task_run_history
from src.monitors import rate_limit
from src.monitors.rate_limit import (
    MIN_HOST_INTERVAL_SECONDS,
)


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

        # 本文件测的是失败分类与退避，不是频率控制。
        # 同主机的最小抓取间隔会让第二轮变成"顺延"，
        # 所以这里把频率检查放开；频率限制另有专门用例覆盖。
        self.limiter_patch = patch.object(
            monitor_dispatcher,
            "reserve_fetch",
            return_value=0,
        )
        self.limiter_patch.start()
        self.addCleanup(self.limiter_patch.stop)

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
            "run_source",
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
            "run_source",
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
            "run_source",
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


    # ---------- 频率控制 ----------

    def test_same_host_is_deferred_not_failed(self):
        # 本用例专门验证频率控制，先撤掉 setUp 里的放开补丁
        self.limiter_patch.stop()

        with patch.object(
            monitor_dispatcher,
            "run_source",
            return_value=0,
        ):
            self.assertEqual(
                monitor_dispatcher.run_due_monitor_tasks(),
                1,
            )

        first = self.state()

        self.assertEqual(first["failure_count"], 0)
        self.assertIsNotNone(first["last_checked_at"])
        self.assertEqual(len(self.history()), 1)

        self.make_due()

        with patch.object(
            monitor_dispatcher,
            "run_source",
            return_value=0,
        ) as monitor:
            # 返回值是"本轮领取到的任务数"，顺延的任务仍然被领取过
            self.assertEqual(
                monitor_dispatcher.run_due_monitor_tasks(),
                1,
            )

        # 第二轮整轮顺延：根本没有去抓
        monitor.assert_not_called()

        second = self.state()

        # 顺延不算失败，也不重置 last_checked_at
        self.assertEqual(second["failure_count"], 0)
        self.assertEqual(
            second["last_checked_at"],
            first["last_checked_at"],
        )
        self.assertIsNone(second["lease_token"])

        delay = (
            datetime.fromisoformat(second["next_run_at"])
            - datetime.now(timezone.utc)
        ).total_seconds()

        self.assertGreater(delay, 0)
        self.assertLessEqual(
            delay, MIN_HOST_INTERVAL_SECONDS + 5
        )

        # 顺延不留执行历史
        self.assertEqual(len(self.history()), 1)

    def test_rate_limit_window(self):
        now = datetime.now(timezone.utc)

        self.assertEqual(
            rate_limit.reserve_fetch(
                "example.com", now=now
            ),
            0,
        )

        wait = rate_limit.reserve_fetch(
            "example.com",
            now=now + timedelta(seconds=10),
        )

        self.assertGreater(wait, 0)

        # 窗口过后重新放行
        self.assertEqual(
            rate_limit.reserve_fetch(
                "example.com",
                now=(
                    now
                    + timedelta(
                        seconds=MIN_HOST_INTERVAL_SECONDS + 1
                    )
                ),
            ),
            0,
        )

        # 不同主机互不影响
        self.assertEqual(
            rate_limit.reserve_fetch(
                "other.example.com", now=now
            ),
            0,
        )

    def test_host_allowlist_blocks_unlisted_source(self):
        with (
            patch.dict(
                os.environ,
                {
                    "RSS_ALLOWED_HOSTS":
                        "other.example.com"
                },
            ),
            patch.object(
                monitor_dispatcher,
                "run_source",
                return_value=0,
            ) as monitor,
        ):
            result = (
                monitor_dispatcher.run_due_monitor_tasks()
            )

        self.assertEqual(result, 1)
        monitor.assert_not_called()

        state = self.state()

        self.assertEqual(state["status"], "paused")
        self.assertIn(
            "未获得执行许可", state["last_error"]
        )


    # ---------- 数据源分发 ----------

    def test_dispatcher_routes_by_source_type(self):
        # 建一个第二种数据源的任务，并用假 handler 替换注册表：
        # 验证调度器是按 task_type 分发，而不是硬编码 RSS。
        second = monitor_registry.create_monitor_task(
            "test_owner",
            "https://example.com/feed",
            30,
            "http_text",
        )

        with closing(
            sqlite3.connect(self.db_path)
        ) as conn, conn:
            conn.execute(
                "UPDATE monitor_tasks SET next_run_at = ?",
                (PAST,),
            )

        seen = []

        def fake_handler(monitor_id, source_url):
            seen.append(monitor_id)
            return 0

        with patch.dict(
            source_registry.SOURCE_HANDLERS,
            {
                "rss": fake_handler,
                "http_text": fake_handler,
            },
        ):
            monitor_dispatcher.run_due_monitor_tasks()

        self.assertIn(self.task_id, seen)
        self.assertIn(second, seen)


if __name__ == "__main__":
    unittest.main()