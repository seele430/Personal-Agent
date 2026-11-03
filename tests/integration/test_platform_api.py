"""D15 续：平台 v1 HTTP API（create / exec / get_exec / cancel / destroy）。

形状与错误码对齐 ``docs/platform-contract.md`` 与
``sdk/fixtures/v1/contracts.json``；exec 是真实 tier1 子进程。
"""

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

TERMINAL = {"succeeded", "failed", "cancelled", "timed_out"}


class PlatformApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)

        patcher = patch.dict(
            "os.environ",
            {
                "PERSONAL_AGENT_SANDBOX_ROOT": str(
                    Path(self.temp.name) / "sandboxes"
                )
            },
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    # ------------------------------------------------------------------

    def create(self, key: str = "create-key-1", **body):
        payload = {
            "tier": "tier1",
            "workspace_id": "ws_demo",
            "ttl_seconds": 600,
            **body,
        }

        return self.client.post(
            "/v1/sandboxes", json=payload, headers={"Idempotency-Key": key}
        )

    def exec(self, sandbox_id: str, command: list[str], **options):
        return self.client.post(
            f"/v1/sandboxes/{sandbox_id}/exec",
            json={"command": command, **options},
        )

    def wait_state(self, exec_id: str, states: set[str], timeout: float = 40.0):
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            body = self.client.get(f"/v1/exec/{exec_id}").json()

            if body["state"] in states:
                return body

            time.sleep(0.05)

        raise AssertionError(f"exec 没有进入 {states}，最后状态 {body['state']}")

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    def test_create_is_idempotent_and_returns_resource(self):
        first = self.create()
        self.assertEqual(first.status_code, 201, first.text)

        body = first.json()
        self.assertTrue(body["sandbox_id"].startswith("sbx_"))
        self.assertEqual(body["state"], "created")
        self.assertEqual(body["node_id"], "node_local")
        self.assertEqual(body["workspace_id"], "ws_demo")
        self.assertEqual(body["ttl_seconds"], 600)
        self.assertTrue(body["expires_at"])

        again = self.create()
        self.assertEqual(again.status_code, 201, again.text)
        self.assertEqual(again.json()["sandbox_id"], body["sandbox_id"])

    def test_missing_idempotency_key_is_rejected(self):
        response = self.client.post(
            "/v1/sandboxes",
            json={"tier": "tier1", "workspace_id": "ws_demo"},
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(
            response.json()["error"]["code"], "INVALID_IDEMPOTENCY_KEY"
        )

    def test_same_key_different_body_conflicts(self):
        self.create()

        response = self.create(workspace_id="ws_other")

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            response.json()["error"]["code"], "IDEMPOTENCY_CONFLICT"
        )

    # ------------------------------------------------------------------
    # exec
    # ------------------------------------------------------------------

    def test_exec_runs_and_returns_output(self):
        sandbox_id = self.create().json()["sandbox_id"]

        accepted = self.exec(
            sandbox_id, [sys.executable, "-c", "print('ok')"]
        )
        self.assertEqual(accepted.status_code, 202, accepted.text)

        body = accepted.json()
        self.assertEqual(body["state"], "queued")
        self.assertTrue(body["exec_id"].startswith("exec_"))

        final = self.wait_state(body["exec_id"], {"succeeded"})

        self.assertEqual(final["exit_code"], 0)
        self.assertIn("ok", final["stdout"])
        self.assertEqual(final["result_code"], "EXEC_SUCCEEDED")
        self.assertFalse(final["truncated"])

    def test_exec_timeout_reports_timed_out(self):
        sandbox_id = self.create().json()["sandbox_id"]

        accepted = self.exec(
            sandbox_id,
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout_ms=600,
        )
        self.assertEqual(accepted.status_code, 202, accepted.text)

        final = self.wait_state(accepted.json()["exec_id"], {"timed_out"})

        self.assertEqual(final["result_code"], "EXEC_TIMED_OUT")

    def test_cancel_stops_exec(self):
        sandbox_id = self.create().json()["sandbox_id"]

        accepted = self.exec(
            sandbox_id,
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout_ms=60000,
        )
        exec_id = accepted.json()["exec_id"]

        self.wait_state(exec_id, {"running"})

        cancelled = self.client.post(f"/v1/exec/{exec_id}/cancel")
        self.assertEqual(cancelled.status_code, 202, cancelled.text)

        final = self.wait_state(exec_id, {"cancelled"})
        self.assertEqual(final["result_code"], "EXEC_CANCELLED")

    def test_exec_rejects_invalid_command(self):
        sandbox_id = self.create().json()["sandbox_id"]

        response = self.exec(sandbox_id, [])

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(
            response.json()["error"]["code"], "INVALID_ARGUMENT"
        )

    def test_unknown_sandbox_and_exec_return_404(self):
        self.assertEqual(
            self.client.get("/v1/sandboxes/sbx_" + "0" * 32).status_code, 404
        )
        self.assertEqual(
            self.client.get("/v1/exec/exec_missing").status_code, 404
        )

    # ------------------------------------------------------------------
    # destroy
    # ------------------------------------------------------------------

    def test_destroy_reclaims_and_blocks_further_exec(self):
        sandbox_id = self.create().json()["sandbox_id"]

        destroyed = self.client.post(f"/v1/sandboxes/{sandbox_id}/destroy")

        self.assertEqual(destroyed.status_code, 200, destroyed.text)
        self.assertEqual(destroyed.json()["state"], "reclaimed")
        self.assertTrue(destroyed.json()["reclaimed_at"])

        # 已回收 → 410 SANDBOX_GONE
        gone = self.client.get(f"/v1/sandboxes/{sandbox_id}")
        self.assertEqual(gone.status_code, 410, gone.text)
        self.assertEqual(gone.json()["error"]["code"], "SANDBOX_GONE")

        blocked = self.exec(sandbox_id, [sys.executable, "-c", "print(1)"])
        self.assertEqual(blocked.status_code, 410, blocked.text)

        # destroy 幂等
        again = self.client.post(f"/v1/sandboxes/{sandbox_id}/destroy")
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["state"], "reclaimed")


if __name__ == "__main__":
    unittest.main()
