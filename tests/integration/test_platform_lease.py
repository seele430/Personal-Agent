"""D18 节点租约：3 个心跳周期无心跳 → not_ready，续约恢复，旧代际不能复活。"""

from __future__ import annotations

import time
import unittest

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.main import app
from src.platform.node_registry import (
    MISSED_HEARTBEATS_ALLOWED,
    NodeRegistry,
    StaleGeneration,
)
from tests.helper import isolate_paths

BASE = datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)


class NodeLeaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)

        patcher = patch.dict(
            "os.environ",
            {
                "PERSONAL_AGENT_SANDBOX_ROOT": str(
                    Path(self.temp.name) / "sandboxes"
                ),
                "PERSONAL_AGENT_PLATFORM_REAPER": "0",
            },
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.registry = NodeRegistry()

    def at(self, seconds: float) -> datetime:
        return BASE + timedelta(seconds=seconds)

    # ------------------------------------------------------------------
    # 租约参数
    # ------------------------------------------------------------------

    def test_registration_exposes_lease_parameters(self):
        node = self.registry.register(
            node_id="node_lease", heartbeat_interval_seconds=2, now=BASE
        )

        self.assertEqual(node["heartbeat_interval_seconds"], 2)
        self.assertEqual(
            node["lease_seconds"], 2 * MISSED_HEARTBEATS_ALLOWED
        )
        self.assertFalse(node["expired"])
        self.assertEqual(node["state"], "ready")
        self.assertTrue(node["lease_expires_at"])

    def test_invalid_heartbeat_interval_is_rejected(self):
        with self.assertRaises(ValueError):
            self.registry.register(node_id="node_bad", heartbeat_interval_seconds=0)

    # ------------------------------------------------------------------
    # 过期判定
    # ------------------------------------------------------------------

    def test_node_stays_ready_until_three_periods_pass(self):
        self.registry.register(
            node_id="node_ttl", heartbeat_interval_seconds=10, now=BASE
        )

        still_ready = self.registry.get("node_ttl", now=self.at(29))
        self.assertEqual(still_ready["state"], "ready")
        self.assertFalse(still_ready["expired"])

        expired = self.registry.get("node_ttl", now=self.at(31))
        self.assertEqual(expired["state"], "not_ready")
        self.assertTrue(expired["expired"])
        self.assertGreaterEqual(expired["seconds_since_heartbeat"], 31)

    def test_heartbeat_renews_lease(self):
        self.registry.register(
            node_id="node_renew", heartbeat_interval_seconds=10, now=BASE
        )
        expired = self.registry.get("node_renew", now=self.at(40))
        self.assertEqual(expired["state"], "not_ready")

        renewed = self.registry.heartbeat(
            "node_renew", generation=1, now=self.at(45)
        )

        self.assertEqual(renewed["state"], "ready")
        self.assertFalse(renewed["expired"])
        self.assertGreater(renewed["lease_expires_at"], expired["lease_expires_at"])

        later = self.registry.get("node_renew", now=self.at(74))
        self.assertEqual(later["state"], "ready")

    def test_stale_generation_cannot_revive_expired_node(self):
        self.registry.register(
            node_id="node_old", heartbeat_interval_seconds=10, now=BASE
        )
        # 重新注册 → generation 2，同时刷新了心跳
        second = self.registry.register(
            node_id="node_old", heartbeat_interval_seconds=10, now=BASE
        )
        self.assertEqual(second["generation"], 2)

        expired = self.registry.get("node_old", now=self.at(40))
        self.assertEqual(expired["state"], "not_ready")

        with self.assertRaises(StaleGeneration):
            self.registry.heartbeat("node_old", generation=1, now=self.at(41))

        # 旧代际的心跳没有续上租约
        after = self.registry.get("node_old", now=self.at(41))
        self.assertEqual(after["state"], "not_ready")
        self.assertEqual(after["last_heartbeat"], expired["last_heartbeat"])


class NodeLeaseApiTest(unittest.TestCase):
    """真实时钟：登记 1 秒心跳周期，等 3 秒以上看 not_ready。"""

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)

        patcher = patch.dict(
            "os.environ",
            {
                "PERSONAL_AGENT_SANDBOX_ROOT": str(
                    Path(self.temp.name) / "sandboxes"
                ),
                "PERSONAL_AGENT_PLATFORM_REAPER": "0",
            },
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def register(self, node_id: str = "node_api") -> dict:
        response = self.client.post(
            "/v1/nodes",
            json={
                "node_id": node_id,
                "capacity": 4,
                "heartbeat_interval_seconds": 1,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_node_goes_not_ready_after_three_missed_heartbeats(self):
        node = self.register()
        self.assertEqual(node["state"], "ready")
        self.assertEqual(node["lease_seconds"], 3)

        time.sleep(3.4)

        listed = self.client.get("/v1/nodes").json()["nodes"][0]
        self.assertEqual(listed["node_id"], "node_api")
        self.assertEqual(listed["state"], "not_ready")
        self.assertTrue(listed["expired"])
        self.assertGreaterEqual(listed["seconds_since_heartbeat"], 3)

    def test_heartbeat_restores_ready(self):
        node = self.register()
        time.sleep(3.4)

        response = self.client.post(
            f"/v1/nodes/{node['node_id']}/heartbeat",
            json={"generation": node["generation"]},
        )

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["state"], "ready")
        self.assertFalse(response.json()["expired"])


if __name__ == "__main__":
    unittest.main()
