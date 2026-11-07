"""控制台写操作的并发竞态：重复取消 / 重复销毁 / 已终态再取消（V2 §7.4）。"""

from __future__ import annotations

import time
import unittest

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.auth_api import get_current_user
from src.main import app
from tests.helper import isolate_paths, wait_for_exec_worker

OWNER = {"id": "u-owner", "username": "tester", "role": "owner"}
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out"}


class ConsoleConcurrencyTest(unittest.TestCase):
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

        app.dependency_overrides[get_current_user] = lambda: OWNER

        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def tearDown(self):
        app.dependency_overrides.pop(get_current_user, None)

    # ------------------------------------------------------------------

    def create_sandbox(self) -> str:
        response = self.client.post(
            "/console/actions/sandboxes",
            json={"workspace_id": "ws_concurrency", "ttl_seconds": 600},
        )
        self.assertEqual(response.status_code, 201, response.text)

        return response.json()["sandbox_id"]

    def submit(self, sandbox_id: str) -> str:
        response = self.client.post(
            "/console/actions/executions",
            json={
                "sandbox_id": sandbox_id,
                "command": ["cmd", "/c", "ping", "-n", "8", "127.0.0.1"],
                "timeout_ms": 60_000,
            },
        )
        self.assertEqual(response.status_code, 202, response.text)

        return response.json()["exec_id"]

    def wait_state(self, exec_id: str, states: set[str], timeout: float = 60.0):
        deadline = time.monotonic() + timeout
        body = {}

        while time.monotonic() < deadline:
            body = self.client.get(f"/console/executions/{exec_id}").json()

            if body.get("state") in states:
                return body

            time.sleep(0.05)

        raise AssertionError(f"exec 没有进入 {states}，最后 {body.get('state')}")

    def parallel(self, count: int, fn):
        with ThreadPoolExecutor(max_workers=count) as pool:
            return list(pool.map(lambda _: fn(), range(count)))

    # ------------------------------------------------------------------

    def test_concurrent_cancel_returns_consistent_responses(self):
        sandbox_id = self.create_sandbox()
        exec_id = self.submit(sandbox_id)

        responses = self.parallel(
            5,
            lambda: self.client.post(
                f"/console/actions/executions/{exec_id}/cancel"
            ),
        )

        codes = [response.status_code for response in responses]

        self.assertEqual(codes, [202] * 5, [r.text for r in responses])

        final = self.wait_state(exec_id, TERMINAL)
        wait_for_exec_worker(exec_id)

        self.assertEqual(final["state"], "cancelled")

    def test_concurrent_destroy_is_idempotent(self):
        sandbox_id = self.create_sandbox()

        responses = self.parallel(
            5,
            lambda: self.client.post(
                f"/console/actions/sandboxes/{sandbox_id}/destroy"
            ),
        )

        codes = [response.status_code for response in responses]

        self.assertEqual(codes, [200] * 5, [r.text for r in responses])

        for response in responses:
            self.assertEqual(response.json()["state"], "reclaimed")

    def test_cancel_after_terminal_is_idempotent(self):
        sandbox_id = self.create_sandbox()
        exec_id = self.submit(sandbox_id)

        self.client.post(f"/console/actions/executions/{exec_id}/cancel")
        self.wait_state(exec_id, TERMINAL)
        wait_for_exec_worker(exec_id)

        responses = self.parallel(
            3,
            lambda: self.client.post(
                f"/console/actions/executions/{exec_id}/cancel"
            ),
        )

        self.assertEqual([r.status_code for r in responses], [202] * 3)

        for response in responses:
            self.assertEqual(response.json()["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
