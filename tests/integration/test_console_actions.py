"""控制台写操作（P1）：权限、闭环、幂等与审计（V2 §7.3 / §7.4）。"""

from __future__ import annotations

import time
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.auth_api import get_current_user
from src.main import app
from src.platform.audit import AuditLog
from src.platform.node_registry import NodeRegistry
from tests.helper import isolate_paths, wait_for_exec_worker

TERMINAL = {"succeeded", "failed", "cancelled", "timed_out"}
OWNER = {"id": "u-owner", "username": "tester", "role": "owner"}
MEMBER = {"id": "u-member", "username": "guest", "role": "member"}


class ConsoleActionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
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

    def tearDown(self):
        app.dependency_overrides.pop(get_current_user, None)

    # ------------------------------------------------------------------

    def as_user(self, user):
        app.dependency_overrides[get_current_user] = lambda: user

    def create_sandbox(self, **body):
        self.as_user(OWNER)

        return self.client.post(
            "/console/actions/sandboxes",
            json={"tier": "tier1", "workspace_id": "ws_demo", "ttl_seconds": 600, **body},
        )

    def wait_state(self, exec_id: str, states: set[str], timeout: float = 40.0):
        deadline = time.monotonic() + timeout
        body = {}

        while time.monotonic() < deadline:
            body = self.client.get(f"/console/executions/{exec_id}").json()

            if body.get("state") in states:
                return body

            time.sleep(0.05)

        raise AssertionError(f"exec 没有进入 {states}，最后状态 {body.get('state')}")

    # ------------------------------------------------------------------

    def test_requires_authentication(self):
        app.dependency_overrides.pop(get_current_user, None)

        response = self.client.post("/console/actions/sandboxes", json={})

        self.assertEqual(response.status_code, 401, response.text)

    def test_member_is_forbidden(self):
        self.as_user(MEMBER)

        response = self.client.post("/console/actions/sandboxes", json={})

        self.assertEqual(response.status_code, 403, response.text)

    def test_full_loop_create_exec_cancel_destroy(self):
        created = self.create_sandbox()

        self.assertEqual(created.status_code, 201, created.text)
        sandbox_id = created.json()["sandbox_id"]
        self.assertEqual(created.json()["state"], "created")

        submitted = self.client.post(
            "/console/actions/executions",
            json={
                "sandbox_id": sandbox_id,
                "command": ["cmd", "/c", "ping", "-n", "6", "127.0.0.1"],
                "timeout_ms": 20_000,
            },
        )

        self.assertEqual(submitted.status_code, 202, submitted.text)
        exec_id = submitted.json()["exec_id"]

        cancelled = self.client.post(
            f"/console/actions/executions/{exec_id}/cancel"
        )

        self.assertEqual(cancelled.status_code, 202, cancelled.text)
        final = self.wait_state(exec_id, TERMINAL)

        self.assertEqual(final["state"], "cancelled")
        wait_for_exec_worker(exec_id)

        destroyed = self.client.post(
            f"/console/actions/sandboxes/{sandbox_id}/destroy"
        )

        self.assertEqual(destroyed.status_code, 200, destroyed.text)
        self.assertEqual(destroyed.json()["state"], "reclaimed")

    def test_cancel_is_idempotent(self):
        sandbox = self.create_sandbox().json()["sandbox_id"]

        exec_id = self.client.post(
            "/console/actions/executions",
            json={
                "sandbox_id": sandbox,
                "command": ["cmd", "/c", "ping", "-n", "6", "127.0.0.1"],
            },
        ).json()["exec_id"]

        first = self.client.post(f"/console/actions/executions/{exec_id}/cancel")
        self.wait_state(exec_id, TERMINAL)
        wait_for_exec_worker(exec_id)

        second = self.client.post(f"/console/actions/executions/{exec_id}/cancel")

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202, second.text)
        self.assertEqual(second.json()["state"], "cancelled")

    def test_destroy_is_idempotent(self):
        sandbox_id = self.create_sandbox().json()["sandbox_id"]

        first = self.client.post(
            f"/console/actions/sandboxes/{sandbox_id}/destroy"
        )
        second = self.client.post(
            f"/console/actions/sandboxes/{sandbox_id}/destroy"
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["state"], "reclaimed")

    def test_cordon_and_drain_change_scheduling(self):
        sandbox = self.create_sandbox().json()
        node_id = sandbox["node_id"] or NodeRegistry().ensure_default_node()["node_id"]

        cordoned = self.client.post(f"/console/actions/nodes/{node_id}/cordon")

        self.assertEqual(cordoned.status_code, 202, cordoned.text)
        self.assertFalse(cordoned.json()["accepting"])

        # cordon 之后再建沙箱应被拒（没有可接收的节点）
        blocked = self.client.post("/console/actions/sandboxes", json={})

        self.assertEqual(blocked.status_code, 429, blocked.text)
        self.assertIn(blocked.json()["error"]["code"], {"NO_READY_NODE", "CAPACITY_EXCEEDED"})

        drained = self.client.post(f"/console/actions/nodes/{node_id}/drain")

        self.assertEqual(drained.status_code, 202, drained.text)
        self.assertTrue(drained.json()["draining"])

    def test_actions_are_audited_with_user_actor(self):
        self.create_sandbox()

        records = AuditLog().list(limit=10, event="sandbox.create")

        self.assertTrue(records)
        self.assertEqual(records[0]["actor"], "user:tester")
        self.assertEqual(records[0]["outcome"], "ok")



    # ------------------------------------------------------------------
    # 错误路径（V2 §7.4：失败也要有可理解的反馈）
    # ------------------------------------------------------------------

    def test_exec_on_unknown_sandbox_is_404(self):
        self.as_user(OWNER)

        response = self.client.post(
            "/console/actions/executions",
            json={"sandbox_id": "sbx_missing", "command": ["cmd", "/c", "echo", "hi"]},
        )

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["error"]["code"], "SANDBOX_NOT_FOUND")

    def test_exec_after_destroy_is_410(self):
        sandbox_id = self.create_sandbox().json()["sandbox_id"]

        self.client.post(f"/console/actions/sandboxes/{sandbox_id}/destroy")

        response = self.client.post(
            "/console/actions/executions",
            json={"sandbox_id": sandbox_id, "command": ["cmd", "/c", "echo", "hi"]},
        )

        self.assertEqual(response.status_code, 410, response.text)
        self.assertEqual(response.json()["error"]["code"], "SANDBOX_GONE")

    def test_invalid_command_is_400(self):
        sandbox_id = self.create_sandbox().json()["sandbox_id"]

        response = self.client.post(
            "/console/actions/executions",
            json={"sandbox_id": sandbox_id, "command": []},
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "INVALID_ARGUMENT")

    def test_cancel_unknown_exec_is_404(self):
        self.as_user(OWNER)

        response = self.client.post("/console/actions/executions/exec_missing/cancel")

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["error"]["code"], "EXEC_NOT_FOUND")

    def test_destroy_unknown_sandbox_is_404(self):
        self.as_user(OWNER)

        response = self.client.post("/console/actions/sandboxes/sbx_missing/destroy")

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["error"]["code"], "SANDBOX_NOT_FOUND")

    def test_cordon_unknown_node_is_404(self):
        self.as_user(OWNER)

        response = self.client.post("/console/actions/nodes/node_missing/cordon")

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["error"]["code"], "NODE_NOT_FOUND")

    def test_queue_full_returns_429_with_retry_after(self):
        with patch.dict(
            "os.environ",
            {
                "PERSONAL_AGENT_PLATFORM_MAX_ACTIVE_EXECS": "1",
                "PERSONAL_AGENT_PLATFORM_QUEUE_SIZE": "1",
            },
        ):
            sandbox_id = self.create_sandbox().json()["sandbox_id"]
            command = ["cmd", "/c", "ping", "-n", "8", "127.0.0.1"]

            first = self.client.post(
                "/console/actions/executions",
                json={"sandbox_id": sandbox_id, "command": command},
            )
            second = self.client.post(
                "/console/actions/executions",
                json={"sandbox_id": sandbox_id, "command": command},
            )
            third = self.client.post(
                "/console/actions/executions",
                json={"sandbox_id": sandbox_id, "command": command},
            )

            self.assertEqual(first.status_code, 202, first.text)
            self.assertEqual(second.status_code, 202, second.text)
            self.assertEqual(third.status_code, 429, third.text)
            self.assertEqual(third.json()["error"]["code"], "QUEUE_FULL")
            self.assertIn("retry-after", {k.lower() for k in third.headers})

            for body in (first.json(), second.json()):
                self.client.post(
                    f"/console/actions/executions/{body['exec_id']}/cancel"
                )
                wait_for_exec_worker(body["exec_id"])

    def test_cancel_after_success_is_409(self):
        sandbox_id = self.create_sandbox().json()["sandbox_id"]

        exec_id = self.client.post(
            "/console/actions/executions",
            json={"sandbox_id": sandbox_id, "command": ["cmd", "/c", "echo", "hi"]},
        ).json()["exec_id"]

        final = self.wait_state(exec_id, TERMINAL)
        wait_for_exec_worker(exec_id)

        self.assertEqual(final["state"], "succeeded")

        response = self.client.post(f"/console/actions/executions/{exec_id}/cancel")

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            response.json()["error"]["code"], "EXEC_NOT_CANCELLABLE"
        )


if __name__ == "__main__":
    unittest.main()
