"""控制台快照：区块 envelope、节点容量口径、窗口指标与部分失败（V2 §4.1 / §5）。

弹性/趋势区块已随弹性平台抽离（2026-11-13，见 docs/STATUS-20261107.md §139），
本文件只保留与弹性无关的通用快照行为。
"""

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
            "viewer",
            "queue",
            "nodes",
            "sandboxes",
            "executions",
        ):
            self.assertIn(key, body)

        for key in ("queue", "nodes", "sandboxes", "executions"):
            self.assertIn("data", body[key], key)
            self.assertIn("error", body[key], key)
            self.assertIsNotNone(body[key]["data"], key)

    def test_cordoned_node_is_not_actual_capacity(self):
        node = NodeRegistry().register(capacity=8)
        NodeRegistry().cordon(node["node_id"])

        body = self.fetch().json()
        nodes = body["nodes"]["data"]

        # 容量口径与原 elastic_controller.capacity_snapshot 相同：
        # cordon 的节点不算可用容量，但单独计数。
        self.assertEqual(nodes["summary"]["nodes_total"], 1)
        self.assertEqual(nodes["summary"]["nodes_accepting"], 0)
        self.assertEqual(nodes["summary"]["nodes_cordoned"], 1)
        self.assertEqual(nodes["summary"]["slots_accepting"], 0)
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

    def test_single_block_failure_does_not_blank_the_page(self):
        with patch("src.console_api._nodes_block", side_effect=RuntimeError("boom")):
            response = self.fetch()

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()

        self.assertIsNone(body["nodes"]["data"])
        self.assertEqual(body["nodes"]["error"]["code"], "BLOCK_FAILED")
        self.assertIn("nodes", body["nodes"]["error"]["message"])
        self.assertIsNotNone(body["queue"]["data"])
        self.assertIsNotNone(body["sandboxes"]["data"])


if __name__ == "__main__":
    unittest.main()
