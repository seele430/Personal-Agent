"""D19 cordon / drain / remove：摘流不杀存量，drain 完才能下线。"""

from __future__ import annotations

import sys
import time
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.main import app
from tests.helper import isolate_paths


class NodeOpsTest(unittest.TestCase):
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

        # 单机默认节点在第一次建沙箱时才登记；这里显式登记，测试不依赖副作用。
        self.client.post(
            "/v1/nodes",
            json={"node_id": "node_local", "capacity": 4},
        )

    # ------------------------------------------------------------------

    def create(self, key: str = "ops-key"):
        return self.client.post(
            "/v1/sandboxes",
            json={
                "tier": "tier1",
                "workspace_id": "ws_ops",
                "ttl_seconds": 600,
            },
            headers={"Idempotency-Key": key},
        )

    def node(self) -> dict:
        nodes = self.client.get("/v1/nodes").json()["nodes"]
        return nodes[0]

    def op(self, action: str, node_id: str = "node_local"):
        return self.client.post(f"/v1/nodes/{node_id}/{action}")

    # ------------------------------------------------------------------
    # cordon
    # ------------------------------------------------------------------

    def test_cordon_blocks_new_sandboxes_but_keeps_state_ready(self):
        sandbox = self.create()
        self.assertEqual(sandbox.status_code, 201, sandbox.text)

        response = self.op("cordon")
        self.assertEqual(response.status_code, 202, response.text)

        body = response.json()
        self.assertTrue(body["cordoned"])
        self.assertFalse(body["accepting"])
        self.assertEqual(body["state"], "ready")  # 租约仍然有效

        blocked = self.create(key="ops-key-2")
        self.assertEqual(blocked.status_code, 429, blocked.text)
        self.assertEqual(blocked.json()["error"]["code"], "NO_READY_NODE")

    def test_cordon_is_idempotent(self):
        first = self.op("cordon").json()
        second = self.op("cordon").json()

        self.assertEqual(second["cordoned"], True)
        self.assertEqual(second["state"], first["state"])

    def test_cordon_does_not_kill_running_exec(self):
        sandbox = self.create().json()["sandbox_id"]

        accepted = self.client.post(
            f"/v1/sandboxes/{sandbox}/exec",
            json={
                "command": [
                    sys.executable, "-c", "import time; time.sleep(1); print('done')"
                ],
                "timeout_ms": 30000,
            },
        )
        exec_id = accepted.json()["exec_id"]

        self.op("cordon")

        deadline = time.monotonic() + 40
        body = {}

        while time.monotonic() < deadline:
            body = self.client.get(f"/v1/exec/{exec_id}").json()
            if body["state"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.05)

        self.assertEqual(body["state"], "succeeded", body)
        self.assertIn("done", body["stdout"])

    # ------------------------------------------------------------------
    # drain / remove
    # ------------------------------------------------------------------

    def test_remove_requires_drain(self):
        response = self.op("remove")

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            response.json()["error"]["code"], "INVALID_STATE_TRANSITION"
        )

    def test_drain_waits_for_sandboxes_before_remove(self):
        sandbox = self.create().json()["sandbox_id"]

        drained = self.op("drain")

        self.assertEqual(drained.status_code, 202, drained.text)
        self.assertTrue(drained.json()["draining"])
        self.assertFalse(drained.json()["accepting"])
        self.assertEqual(drained.json()["active_sandboxes"], 1)

        blocked = self.create(key="drain-blocked")
        self.assertEqual(blocked.status_code, 429, blocked.text)

        busy = self.op("remove")
        self.assertEqual(busy.status_code, 409, busy.text)
        self.assertTrue(busy.json()["error"]["retryable"])
        self.assertEqual(
            busy.json()["error"]["code"], "INVALID_STATE_TRANSITION"
        )

        # 存量回收后才允许下线
        self.client.post(f"/v1/sandboxes/{sandbox}/destroy")
        removed = self.op("remove")

        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertTrue(removed.json()["removed"])
        self.assertFalse(removed.json()["accepting"])
        self.assertEqual(removed.json()["active_sandboxes"], 0)

    def test_removed_node_rejects_heartbeat_and_new_ops(self):
        self.op("drain")
        self.assertEqual(self.op("remove").status_code, 200)

        heartbeat = self.client.post(
            "/v1/nodes/node_local/heartbeat", json={"generation": 1}
        )
        self.assertEqual(heartbeat.status_code, 409, heartbeat.text)
        self.assertEqual(
            heartbeat.json()["error"]["code"], "INVALID_STATE_TRANSITION"
        )

        again = self.op("cordon")
        self.assertEqual(again.status_code, 409, again.text)

    def test_active_sandbox_count_is_visible(self):
        first = self.create(key="count-1").json()["sandbox_id"]
        self.create(key="count-2")

        self.assertEqual(self.node()["active_sandboxes"], 2)

        self.client.post(f"/v1/sandboxes/{first}/destroy")

        self.assertEqual(self.node()["active_sandboxes"], 1)


if __name__ == "__main__":
    unittest.main()
