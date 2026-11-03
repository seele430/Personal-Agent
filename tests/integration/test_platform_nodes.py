"""D17 节点注册与心跳：generation 单调、旧代际被拒绝、节点列表有 last_heartbeat。"""

from __future__ import annotations

import time
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.main import app
from src.platform.node_registry import NodeNotFound, NodeRegistry, StaleGeneration
from tests.helper import isolate_paths


class PlatformNodesTest(unittest.TestCase):
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

    # ------------------------------------------------------------------

    def register(self, node_id: str = "node_test", capacity: int = 4):
        return self.client.post(
            "/v1/nodes", json={"node_id": node_id, "capacity": capacity}
        )

    def heartbeat(self, node_id: str, generation: int):
        return self.client.post(
            f"/v1/nodes/{node_id}/heartbeat", json={"generation": generation}
        )

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def test_register_allocates_generation_and_heartbeat(self):
        response = self.register()

        self.assertEqual(response.status_code, 201, response.text)

        body = response.json()
        self.assertEqual(body["node_id"], "node_test")
        self.assertEqual(body["generation"], 1)
        self.assertEqual(body["state"], "ready")
        self.assertEqual(body["capacity"], 4)
        self.assertTrue(body["last_heartbeat"])
        self.assertTrue(body["registered_at"])

    def test_reregister_bumps_generation(self):
        first = self.register().json()
        second = self.register().json()

        self.assertEqual(first["generation"], 1)
        self.assertEqual(second["generation"], 2)

    def test_invalid_capacity_is_rejected(self):
        response = self.register(capacity=0)

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(
            response.json()["error"]["code"], "INVALID_ARGUMENT"
        )

    # ------------------------------------------------------------------
    # 心跳
    # ------------------------------------------------------------------

    def test_heartbeat_updates_last_heartbeat(self):
        node = self.register().json()
        first = node["last_heartbeat"]
        time.sleep(0.02)

        response = self.heartbeat(node["node_id"], node["generation"])

        self.assertEqual(response.status_code, 202, response.text)
        self.assertGreater(response.json()["last_heartbeat"], first)

    def test_stale_generation_is_rejected(self):
        self.register()  # generation 1
        current = self.register().json()  # generation 2

        response = self.heartbeat(current["node_id"], generation=1)

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            response.json()["error"]["code"], "NODE_GENERATION_STALE"
        )

        # 旧代际不能续新实例的租约：last_heartbeat 没被改写
        row = NodeRegistry().get(current["node_id"])
        self.assertEqual(row["last_heartbeat"], current["last_heartbeat"])

    def test_heartbeat_unknown_node_returns_404(self):
        response = self.heartbeat("node_missing", generation=1)

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["error"]["code"], "NODE_NOT_FOUND")

    # ------------------------------------------------------------------
    # 列表与默认节点
    # ------------------------------------------------------------------

    def test_list_includes_registered_nodes(self):
        self.register(node_id="node_a")
        self.register(node_id="node_b")

        response = self.client.get("/v1/nodes")

        self.assertEqual(response.status_code, 200, response.text)
        ids = [node["node_id"] for node in response.json()["nodes"]]
        self.assertIn("node_a", ids)
        self.assertIn("node_b", ids)

        node = next(
            item for item in response.json()["nodes"]
            if item["node_id"] == "node_a"
        )
        self.assertTrue(node["last_heartbeat"])
        self.assertEqual(node["generation"], 1)

    def test_creating_sandbox_registers_default_node(self):
        # 建沙箱之前节点表是空的
        self.assertEqual(self.client.get("/v1/nodes").json()["nodes"], [])

        sandbox = self.client.post(
            "/v1/sandboxes",
            json={
                "tier": "tier1",
                "workspace_id": "ws_nodes",
                "ttl_seconds": 600,
            },
            headers={"Idempotency-Key": "nodes-key"},
        ).json()

        self.assertEqual(sandbox["node_id"], "node_local")

        nodes = self.client.get("/v1/nodes").json()["nodes"]
        self.assertEqual([node["node_id"] for node in nodes], ["node_local"])
        self.assertTrue(nodes[0]["last_heartbeat"])

    # ------------------------------------------------------------------
    # 仓储层直接调用
    # ------------------------------------------------------------------

    def test_registry_errors_are_typed(self):
        registry = NodeRegistry()

        with self.assertRaises(NodeNotFound):
            registry.heartbeat("node_missing", generation=1)

        registry.register(node_id="node_c")
        registry.register(node_id="node_c")

        with self.assertRaises(StaleGeneration):
            registry.heartbeat("node_c", generation=1)


if __name__ == "__main__":
    unittest.main()
