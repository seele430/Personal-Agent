"""D20 沙箱状态机：created → running ↔ idle、failed、reclaimed 与非法迁移。

契约见 ``docs/platform-contract.md`` 4.2。顶层用服务层直接驱动合法/非法迁移，
API 层再验「exec 让沙箱进入 running、结束后回到 idle」。
"""

from __future__ import annotations

import sqlite3
import sys
import time
import unittest

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.main import app
from src.platform.sandbox_service import (
    InvalidTransition,
    SandboxService,
)
from tests.helper import isolate_paths

OLD_SCHEMA = """
CREATE TABLE agent_sandboxes (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    tier TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('created', 'reclaimed')),
    ttl_seconds INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    reclaimed_at TEXT
);
CREATE TABLE sandbox_idempotency (
    key TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    sandbox_id TEXT NOT NULL REFERENCES agent_sandboxes(id),
    created_at TEXT NOT NULL
);
INSERT INTO agent_sandboxes VALUES (
    'sbx_legacy0000000000000000000000', 'ws_legacy', 'tier1', 'created',
    600, '2026-11-03T00:00:00+00:00', '2026-11-03T00:10:00+00:00', NULL
);
"""


class SandboxStateServiceTest(unittest.TestCase):
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

        self.service = SandboxService(
            Path(self.temp.name) / "platform.db",
            Path(self.temp.name) / "sandboxes",
        )

    def make(self, key: str = "state-key") -> str:
        return self.service.create(idempotency_key=key)["id"]

    # ------------------------------------------------------------------

    def test_legal_chain_created_running_idle_reclaimed(self):
        sandbox_id = self.make()

        self.assertEqual(self.service.get(sandbox_id)["state"], "created")
        self.assertEqual(
            self.service.begin_exec(sandbox_id)["state"], "running"
        )
        self.assertEqual(
            self.service.end_exec(sandbox_id, active_execs=0)["state"], "idle"
        )
        self.assertEqual(
            self.service.begin_exec(sandbox_id)["state"], "running"
        )

        reclaimed = self.service.reclaim(sandbox_id, reason="manual")
        self.assertEqual(reclaimed["state"], "reclaimed")
        self.assertEqual(reclaimed["reclaim_reason"], "manual")

        # 重复回收幂等
        again = self.service.reclaim(sandbox_id)
        self.assertEqual(again["state"], "reclaimed")

    def test_failed_can_only_be_reclaimed(self):
        sandbox_id = self.make()

        failed = self.service.mark_failed(sandbox_id, "runner crashed")
        self.assertEqual(failed["state"], "failed")

        with self.assertRaises(InvalidTransition):
            self.service.transition(sandbox_id, "running")

        self.assertEqual(
            self.service.reclaim(sandbox_id)["state"], "reclaimed"
        )

    def test_illegal_transitions_are_rejected(self):
        sandbox_id = self.make()

        # created 不能直接 idle / failed→running / reclaimed 任何迁移
        with self.assertRaises(InvalidTransition) as raised:
            self.service.transition(sandbox_id, "idle")

        self.assertEqual(raised.exception.code, "INVALID_STATE_TRANSITION")
        self.assertEqual(raised.exception.http_status, 409)

        # 同一状态重复迁移是幂等的，不算非法
        self.assertEqual(
            self.service.transition(sandbox_id, "created")["state"], "created"
        )

        self.service.reclaim(sandbox_id)

        for target in ("created", "running", "idle", "failed"):
            with self.subTest(target=target):
                with self.assertRaises(InvalidTransition):
                    self.service.transition(sandbox_id, target)

    def test_begin_exec_rejects_reclaimed_sandbox(self):
        sandbox_id = self.make()
        self.service.reclaim(sandbox_id)

        with self.assertRaises(InvalidTransition):
            self.service.begin_exec(sandbox_id)

    def test_concurrent_execs_keep_state_running(self):
        sandbox_id = self.make()

        self.service.begin_exec(sandbox_id)
        self.service.begin_exec(sandbox_id)  # 第二个并发 exec 仍然合法

        # 还有一个活动 exec：不能回 idle
        self.assertEqual(
            self.service.end_exec(sandbox_id, active_execs=1)["state"],
            "running",
        )
        self.assertEqual(
            self.service.end_exec(sandbox_id, active_execs=0)["state"], "idle"
        )


class SandboxStateApiTest(unittest.TestCase):
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

    def create(self, key: str = "api-state-key") -> str:
        response = self.client.post(
            "/v1/sandboxes",
            json={
                "tier": "tier1",
                "workspace_id": "ws_state",
                "ttl_seconds": 600,
            },
            headers={"Idempotency-Key": key},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["sandbox_id"]

    def wait_state(self, sandbox_id: str, wanted: str, timeout: float = 30.0):
        deadline = time.monotonic() + timeout
        state = None

        while time.monotonic() < deadline:
            state = self.client.get(f"/v1/sandboxes/{sandbox_id}").json()["state"]
            if state == wanted:
                return state
            time.sleep(0.02)

        raise AssertionError(f"沙箱状态没有变成 {wanted}，当前 {state}")

    def test_exec_moves_sandbox_running_then_idle(self):
        sandbox_id = self.create()

        accepted = self.client.post(
            f"/v1/sandboxes/{sandbox_id}/exec",
            json={
                "command": [
                    sys.executable,
                    "-c",
                    "import time; time.sleep(0.6); print('done')",
                ],
                "timeout_ms": 30000,
            },
        )
        exec_id = accepted.json()["exec_id"]

        self.wait_state(sandbox_id, "running")

        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if self.client.get(f"/v1/exec/{exec_id}").json()["state"] in {
                "succeeded",
                "failed",
                "cancelled",
                "timed_out",
            }:
                break
            time.sleep(0.05)

        self.wait_state(sandbox_id, "idle")

    def test_exec_on_failed_sandbox_returns_409(self):
        sandbox_id = self.create()
        SandboxService().mark_failed(sandbox_id, "runner crashed")

        response = self.client.post(
            f"/v1/sandboxes/{sandbox_id}/exec",
            json={"command": [sys.executable, "-c", "print(1)"]},
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            response.json()["error"]["code"], "INVALID_STATE_TRANSITION"
        )


class SandboxStateMigrationTest(unittest.TestCase):
    """D20 之前建的库只允许 created/reclaimed，必须能原地升级。"""

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

    def test_legacy_table_is_rebuilt_and_backfilled(self):
        db_path = Path(self.temp.name) / "platform.db"

        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.executescript(OLD_SCHEMA)

        service = SandboxService(db_path, Path(self.temp.name) / "sandboxes")

        legacy = service.get("sbx_legacy0000000000000000000000")
        self.assertEqual(legacy["state"], "created")
        self.assertEqual(legacy["node_id"], "node_local")  # D19 回填

        # 新状态可写入（旧 CHECK 只允许 created/reclaimed）
        self.assertEqual(
            service.begin_exec(legacy["id"])["state"], "running"
        )
        self.assertEqual(
            service.end_exec(legacy["id"], active_execs=0)["state"], "idle"
        )

        # 幂等表仍然指向重建后的表
        with closing(sqlite3.connect(db_path)) as conn:
            key = conn.execute(
                "SELECT COUNT(*) FROM sandbox_idempotency"
            ).fetchone()[0]

        self.assertEqual(key, 0)

        # 迁移可重复执行
        SandboxService(db_path, Path(self.temp.name) / "sandboxes")


if __name__ == "__main__":
    unittest.main()
