"""D30 有界队列：超额 429 QUEUE_FULL、排队可取消、名额释放后自动放行。"""

from __future__ import annotations

import sys
import time
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.main import app
from src.platform import exec_queue
from src.platform.exec_store import ExecStore
from tests.helper import isolate_paths, wait_for_exec_worker

SLEEP = [sys.executable, "-c", "import time; time.sleep(2)"]
QUICK = [sys.executable, "-c", "print('quick')"]


class ExecQueueApiTest(unittest.TestCase):
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
                "PERSONAL_AGENT_PLATFORM_MAX_ACTIVE_EXECS": "1",
                "PERSONAL_AGENT_PLATFORM_QUEUE_SIZE": "1",
            },
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        exec_queue.reset_queue()

        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

        created = self.client.post(
            "/v1/sandboxes",
            json={
                "tier": "tier1",
                "workspace_id": "ws_queue",
                "ttl_seconds": 600,
            },
            headers={"Idempotency-Key": "queue-key"},
        )
        self.sandbox_id = created.json()["sandbox_id"]

    # ------------------------------------------------------------------

    def tearDown(self):
        # 收尾：把还活着的 exec 取消掉并等 worker 退场，否则临时目录会被进程占住。
        try:
            rows = ExecStore().list_by_sandbox(self.sandbox_id)
        except Exception:  # noqa: BLE001 - 清理阶段
            rows = []

        for row in rows:
            if row["state"] in {"queued", "running"}:
                self.client.post(f"/v1/exec/{row['id']}/cancel")

        for row in rows:
            wait_for_exec_worker(row["id"])

    def submit(self, command):
        return self.client.post(
            f"/v1/sandboxes/{self.sandbox_id}/exec",
            json={"command": command, "timeout_ms": 30000},
        )

    def wait_state(self, exec_id, states, timeout=20.0):
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            body = self.client.get(f"/v1/exec/{exec_id}").json()
            if body["state"] in states:
                return body
            time.sleep(0.02)

        self.fail(f"exec {exec_id} 没有进入 {states}")

    # ------------------------------------------------------------------

    def test_queue_full_returns_429_without_creating_exec(self):
        first = self.submit(SLEEP)
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(first.json()["state"], "queued")

        second = self.submit(QUICK)
        self.assertEqual(second.status_code, 202, second.text)
        self.assertEqual(second.json()["state"], "queued")

        before = len(ExecStore().list_by_sandbox(self.sandbox_id))
        third = self.submit(QUICK)

        self.assertEqual(third.status_code, 429, third.text)
        self.assertEqual(third.json()["error"]["code"], "QUEUE_FULL")
        self.assertTrue(third.json()["error"]["retryable"])
        self.assertEqual(len(ExecStore().list_by_sandbox(self.sandbox_id)), before)

    def test_queued_exec_starts_after_slot_frees(self):
        self.submit(SLEEP)
        second = self.submit(QUICK).json()["exec_id"]

        self.assertEqual(
            self.client.get(f"/v1/exec/{second}").json()["state"], "queued"
        )

        running = self.wait_state(second, {"running", "succeeded"})
        self.assertIn(running["state"], {"running", "succeeded"})

        done = self.wait_state(second, {"succeeded", "failed", "cancelled"})
        self.assertEqual(done["state"], "succeeded")
        self.assertIn("quick", done["stdout"])

    def test_queued_exec_can_be_cancelled(self):
        first = self.submit(SLEEP).json()["exec_id"]
        second = self.submit(SLEEP).json()["exec_id"]

        cancelled = self.client.post(f"/v1/exec/{second}/cancel")

        self.assertEqual(cancelled.status_code, 202, cancelled.text)
        self.assertEqual(cancelled.json()["state"], "cancelled")
        self.assertEqual(cancelled.json()["result_code"], "EXEC_CANCELLED")
        # 从未起过进程，所以没有输出
        self.assertEqual(cancelled.json()["stdout"], "")

        # 取消排队项不占用并发名额：应该能再排一个
        third = self.submit(QUICK)
        self.assertEqual(third.status_code, 202, third.text)

        self.client.post(f"/v1/exec/{first}/cancel")
        wait_for_exec_worker(first)


class ExecQueueUnitTest(unittest.TestCase):
    def test_submit_finish_and_cancel(self):
        started: list[str] = []
        queue = exec_queue.ExecQueue(max_active=1, max_queued=1)
        queue.start_with(lambda item: started.append(item.exec_id))

        def item(name):
            return exec_queue.QueuedExec(
                exec_id=name,
                sandbox_id="sbx_" + "0" * 32,
                argv=["x"],
                timeout_ms=1000,
                max_output_bytes=100,
            )

        self.assertEqual(queue.submit(item("a")), "started")
        self.assertEqual(queue.submit(item("b")), "queued")
        self.assertEqual(queue.submit(item("c")), "full")
        self.assertEqual(started, ["a"])

        self.assertTrue(queue.cancel("b"))
        self.assertFalse(queue.cancel("b"))

        queue.finish()
        self.assertEqual(queue.snapshot()["active"], 0)
        self.assertEqual(queue.snapshot()["queued"], 0)

    def test_finish_releases_next_queued(self):
        started: list[str] = []
        queue = exec_queue.ExecQueue(max_active=1, max_queued=2)
        queue.start_with(lambda item: started.append(item.exec_id))

        def item(name):
            return exec_queue.QueuedExec(
                exec_id=name,
                sandbox_id="sbx_" + "0" * 32,
                argv=["x"],
                timeout_ms=1000,
                max_output_bytes=100,
            )

        queue.submit(item("a"))
        queue.submit(item("b"))
        queue.submit(item("c"))
        queue.finish()

        self.assertEqual(started, ["a", "b"])
        self.assertEqual(queue.snapshot(), {
            "active": 1,
            "queued": 1,
            "max_active": 1,
            "max_queued": 2,
        })


if __name__ == "__main__":
    unittest.main()
