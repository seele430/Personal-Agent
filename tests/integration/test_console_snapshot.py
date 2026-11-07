"""控制台快照：区块 envelope、弹性口径、窗口指标与部分失败（V2 §4.1 / §5）。"""

from __future__ import annotations

import sqlite3
import unittest

from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import config
from src.auth_api import get_current_user
from src.main import app
from src.platform.exec_store import ExecStore
from src.platform.node_registry import NodeRegistry
from tests.helper import isolate_paths


def _iso(offset_seconds: float) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    ).isoformat()


class ConsoleSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)

        # 采样线程在测试里默认关闭（tests.helper 设 PERSONAL_AGENT_TRENDS=0），
        # 样本由用例自己喂，避免受到启动时那次采样的干扰。
        from src.platform import trends

        trends.reset()
        self.addCleanup(trends.reset)

        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def tearDown(self):
        app.dependency_overrides.pop(get_current_user, None)

    def authenticate(self):
        app.dependency_overrides[get_current_user] = lambda: {
            "id": "u1",
            "username": "tester",
            "role": "owner",
        }

    def fetch(self, **params):
        self.authenticate()

        return self.client.get("/console/snapshot", params=params)

    # ------------------------------------------------------------------

    def test_requires_authentication(self):
        response = self.client.get("/console/snapshot")

        self.assertEqual(response.status_code, 401, response.text)

    def test_blocks_use_envelope(self):
        body = self.fetch().json()

        for key in (
            "generated_at",
            "series_window",
            "sample_count",
            "data_source",
            "is_stale",
            "queue",
            "nodes",
            "sandboxes",
            "executions",
            "elastic",
            "trends",
        ):
            self.assertIn(key, body)

        self.assertEqual(body["series_window"], "15m")
        self.assertEqual(body["sample_count"], 0)
        self.assertEqual(body["data_source"], "live")

        for key in ("queue", "nodes", "sandboxes", "executions", "elastic"):
            self.assertIn("data", body[key], key)
            self.assertIn("error", body[key], key)
            self.assertIsNotNone(body[key]["data"], key)

    def test_trends_report_series_and_sample_count(self):
        import time as _time

        from src.platform import trends

        self.addCleanup(trends.reset)

        now = _time.time()

        trends.record_sample(
            running=3, queued=2, capacity=2, desired=3, action="scale_out", at=now - 120
        )
        trends.record_sample(
            running=1, queued=0, capacity=2, desired=1, action="hold", at=now - 60
        )

        body = self.fetch().json()
        data = body["trends"]["data"]

        self.assertEqual(data["sample_count"], 2)
        self.assertEqual(body["sample_count"], 2)
        self.assertEqual(len(data["series"]), 2)
        self.assertFalse(data["persisted"])
        self.assertIn("重启", data["note"])
        self.assertEqual(data["series"][0]["action"], "scale_out")

        # 数据年龄：最新样本约 60 秒、最旧约 120 秒（V2 §6.1-5）
        self.assertGreaterEqual(data["latest_sample_age_seconds"], 55)
        self.assertLessEqual(data["latest_sample_age_seconds"], 75)
        self.assertGreaterEqual(data["oldest_sample_age_seconds"], 115)
        self.assertLessEqual(data["oldest_sample_age_seconds"], 135)

    def test_trends_drop_samples_outside_window(self):
        import time as _time

        from src.platform import trends

        self.addCleanup(trends.reset)

        now = _time.time()

        trends.record_sample(
            running=9, queued=9, capacity=0, desired=1, at=now - 2000
        )
        trends.record_sample(
            running=1, queued=0, capacity=0, desired=1, at=now - 10
        )

        data = self.fetch().json()["trends"]["data"]

        self.assertEqual(data["sample_count"], 1)
        self.assertEqual(data["series"][0]["running"], 1)

    def test_elastic_without_controller_is_compute_only(self):
        body = self.fetch().json()

        elastic = body["elastic"]["data"]

        self.assertEqual(elastic["controller_mode"], "not_running")
        self.assertFalse(elastic["controller_running"])
        self.assertEqual(elastic["desired_capacity"], 1)
        self.assertEqual(elastic["actual_capacity"], 0)
        self.assertEqual(elastic["capacity_unit"], "nodes")
        self.assertIn("未驱动真实容量", elastic["message"])
        self.assertEqual(elastic["stabilization"]["required_samples"], 2)
        self.assertIsNone(elastic["stabilization"]["satisfied_count"])
        self.assertEqual(elastic["events"], [])

    def test_cordoned_node_is_not_actual_capacity(self):
        node = NodeRegistry().register(capacity=8)
        NodeRegistry().cordon(node["node_id"])

        body = self.fetch().json()
        elastic = body["elastic"]["data"]
        nodes = body["nodes"]["data"]

        self.assertEqual(elastic["actual_capacity"], 0)
        self.assertEqual(elastic["capacity"]["nodes_total"], 1)
        self.assertEqual(elastic["capacity"]["nodes_cordoned"], 1)
        self.assertEqual(nodes["rows"][0]["accepting"], False)

    def test_window_metrics_follow_documented_denominator(self):
        ExecStore()

        rows = [
            ("e1", "succeeded"),
            ("e2", "succeeded"),
            ("e3", "succeeded"),
            ("e4", "failed"),
            ("e5", "timed_out"),
            ("e6", "cancelled"),
            ("e7", "cancelled"),
        ]

        conn = sqlite3.connect(config.paths.platform_db)

        with conn:
            for exec_id, state in rows:
                conn.execute(
                    """
                    INSERT INTO platform_execs(
                        id, sandbox_id, state, argv_json, timeout_ms,
                        max_output_bytes, truncated, created_at,
                        started_at, finished_at, exit_code
                    ) VALUES (?, 'sbx', ?, '[]', 1000, 4096, 0, ?, ?, ?, 0)
                    """,
                    (exec_id, state, _iso(-12), _iso(-11), _iso(-1)),
                )

        conn.close()

        body = self.fetch().json()
        window = body["executions"]["data"]["window"]

        self.assertEqual(window["samples"], 7)
        self.assertEqual(window["terminal"]["cancelled"], 2)
        self.assertEqual(window["success_rate"]["denominator"], 5)
        self.assertEqual(window["success_rate"]["numerator"], 3)
        self.assertEqual(window["success_rate"]["value"], 0.6)
        self.assertEqual(window["success_rate"]["excluded_cancelled"], 2)
        self.assertEqual(window["queue_wait_ms"]["samples"], 7)
        self.assertTrue(window["queue_wait_ms"]["insufficient_samples"])
        self.assertIsNone(window["queue_wait_ms"]["p50"])

        # 同一窗口的执行结果也要出现在趋势块里
        self.assertTrue(body["trends"]["data"]["exec_buckets"])

    def test_single_block_failure_does_not_blank_the_page(self):
        with patch("src.console_api._nodes_block", side_effect=RuntimeError("boom")):
            response = self.fetch()

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()

        self.assertIsNone(body["nodes"]["data"])
        self.assertEqual(body["nodes"]["error"]["code"], "BLOCK_FAILED")
        self.assertIn("nodes", body["nodes"]["error"]["message"])
        self.assertIsNotNone(body["elastic"]["data"])
        self.assertIsNotNone(body["sandboxes"]["data"])


if __name__ == "__main__":
    unittest.main()
