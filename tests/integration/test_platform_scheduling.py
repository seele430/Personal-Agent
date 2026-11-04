"""最小 least-loaded 调度与单用户全局上限（修订版 D19 / D21 口径）。"""

from __future__ import annotations

import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.main import app
from tests.helper import isolate_paths


class SchedulingTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
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

    def register(self, node_id: str, capacity: int = 4):
        response = self.client.post(
            "/v1/nodes",
            json={"node_id": node_id, "capacity": capacity},
        )
        self.assertEqual(response.status_code, 201, response.text)

    def create(self, key: str):
        return self.client.post(
            "/v1/sandboxes",
            json={
                "tier": "tier1",
                "workspace_id": f"ws_{key}",
                "ttl_seconds": 600,
            },
            headers={"Idempotency-Key": key},
        )

    def test_least_loaded_round_robin(self):
        self.register("node_a")
        self.register("node_b")

        first = self.create("sched-1").json()["node_id"]
        second = self.create("sched-2").json()["node_id"]

        self.assertEqual(first, "node_a")  # 并列时按 node_id 稳定选择
        self.assertEqual(second, "node_b")  # node_a 已有 1 个活动沙箱

    def test_cordoned_node_is_skipped(self):
        self.register("node_a")
        self.register("node_b")
        self.client.post("/v1/nodes/node_a/cordon")

        placed = self.create("sched-3").json()["node_id"]

        self.assertEqual(placed, "node_b")

    def test_capacity_full_returns_capacity_exceeded(self):
        # 先建一个沙箱把默认节点实物化，再注册两个容量 1 的节点并摘掉默认节点。
        self.create("cap-warm")
        self.register("node_a", capacity=1)
        self.register("node_b", capacity=1)
        self.client.post("/v1/nodes/node_local/cordon")

        self.assertEqual(self.create("cap-a").json()["node_id"], "node_a")
        self.assertEqual(self.create("cap-b").json()["node_id"], "node_b")

        blocked = self.create("cap-c")

        self.assertEqual(blocked.status_code, 429, blocked.text)
        self.assertEqual(
            blocked.json()["error"]["code"], "CAPACITY_EXCEEDED"
        )

    def test_single_user_global_limit(self):
        with patch.dict(
            "os.environ",
            {"PERSONAL_AGENT_SANDBOX_GLOBAL_LIMIT": "1"},
        ):
            first = self.create("global-1")
            second = self.create("global-2")

        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(second.status_code, 429, second.text)
        self.assertEqual(
            second.json()["error"]["code"], "CAPACITY_EXCEEDED"
        )

    def test_global_limit_releases_after_destroy(self):
        with patch.dict(
            "os.environ",
            {"PERSONAL_AGENT_SANDBOX_GLOBAL_LIMIT": "1"},
        ):
            created = self.create("global-3")
            self.assertEqual(created.status_code, 201, created.text)

            self.client.post(
                f"/v1/sandboxes/{created.json()['sandbox_id']}/destroy"
            )

            again = self.create("global-4")

        self.assertEqual(again.status_code, 201, again.text)


if __name__ == "__main__":
    unittest.main()
