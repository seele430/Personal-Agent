"""D15 幂等 create：同 key 重试 10 次只建一个沙箱。

契约见 ``docs/platform-contract.md`` 3.1。这里不经过 HTTP：直接验证控制面
服务 + SQLite 的唯一约束与事务语义。
"""

from __future__ import annotations

import sqlite3
import unittest

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.platform.sandbox_service import (
    DEFAULT_TTL_SECONDS,
    IdempotencyConflict,
    InvalidIdempotencyKey,
    SandboxNotFound,
    SandboxService,
)
from tests.helper import isolate_paths


class SandboxCreateTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)

        self.root = Path(self.temp.name) / "sandboxes"
        patcher = patch.dict(
            "os.environ",
            {"PERSONAL_AGENT_SANDBOX_ROOT": str(self.root)},
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.db = Path(self.temp.name) / "platform.db"
        self.service = SandboxService(self.db, self.root)

    # ------------------------------------------------------------------

    def count(self, table: str) -> int:
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    # ------------------------------------------------------------------
    # 同 key 重试
    # ------------------------------------------------------------------

    def test_same_key_ten_retries_creates_one_sandbox(self):
        first = self.service.create(
            idempotency_key="retry-key-1", workspace_id="ws_demo"
        )

        seen = {first["id"]}
        self.assertTrue(first["created"])

        for _ in range(9):
            again = self.service.create(
                idempotency_key="retry-key-1", workspace_id="ws_demo"
            )
            self.assertFalse(again["created"])
            self.assertEqual(again["id"], first["id"])
            seen.add(again["id"])

        self.assertEqual(len(seen), 1)
        self.assertEqual(self.count("agent_sandboxes"), 1)
        self.assertEqual(self.count("sandbox_idempotency"), 1)
        self.assertTrue(Path(first["workspace"]).is_dir())

    def test_defaults_are_normalized(self):
        first = self.service.create(idempotency_key="defaults")
        again = self.service.create(
            idempotency_key="defaults", ttl_seconds=DEFAULT_TTL_SECONDS
        )

        self.assertEqual(first["id"], again["id"])
        self.assertFalse(again["created"])

    def test_idempotency_survives_restart(self):
        first = self.service.create(
            idempotency_key="restart-key", workspace_id="ws_restart"
        )

        restarted = SandboxService(self.db, self.root)
        again = restarted.create(
            idempotency_key="restart-key", workspace_id="ws_restart"
        )

        self.assertEqual(first["id"], again["id"])
        self.assertFalse(again["created"])

    # ------------------------------------------------------------------
    # 同 key 不同请求体
    # ------------------------------------------------------------------

    def test_same_key_different_body_conflicts(self):
        self.service.create(idempotency_key="conflict-key", workspace_id="ws_a")

        with self.assertRaises(IdempotencyConflict) as raised:
            self.service.create(
                idempotency_key="conflict-key", workspace_id="ws_b"
            )

        self.assertEqual(raised.exception.code, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(raised.exception.http_status, 409)
        self.assertEqual(self.count("agent_sandboxes"), 1)

    def test_ttl_change_counts_as_different_request(self):
        self.service.create(idempotency_key="ttl-key")

        with self.assertRaises(IdempotencyConflict):
            self.service.create(idempotency_key="ttl-key", ttl_seconds=1200)

    # ------------------------------------------------------------------
    # 并发
    # ------------------------------------------------------------------

    def test_concurrent_create_only_one_sandbox(self):
        def create(_index: int) -> str:
            return self.service.create(
                idempotency_key="concurrent-key", workspace_id="ws_c"
            )["id"]

        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(create, range(16)))

        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(self.count("agent_sandboxes"), 1)
        self.assertEqual(self.count("sandbox_idempotency"), 1)

    # ------------------------------------------------------------------
    # 参数与回收
    # ------------------------------------------------------------------

    def test_invalid_keys_are_rejected(self):
        for key in ("", "bad key", "x" * 129, "中文键"):
            with self.subTest(key=key):
                with self.assertRaises(InvalidIdempotencyKey):
                    self.service.create(idempotency_key=key)

    def test_invalid_ttl_and_tier_are_rejected(self):
        from src.platform.sandbox_service import InvalidArgument

        with self.assertRaises(InvalidArgument):
            self.service.create(idempotency_key="bad-ttl", ttl_seconds=0)

        with self.assertRaises(InvalidArgument):
            self.service.create(idempotency_key="bad-tier", tier="tier2")

    def test_reclaim_is_idempotent_and_removes_workspace(self):
        sandbox = self.service.create(
            idempotency_key="reclaim-key", workspace_id="ws_reclaim"
        )
        workspace = Path(sandbox["workspace"])
        self.assertTrue(workspace.is_dir())

        first = self.service.reclaim(sandbox["id"])
        second = self.service.reclaim(sandbox["id"])

        self.assertEqual(first["state"], "reclaimed")
        self.assertEqual(second["state"], "reclaimed")
        self.assertFalse(workspace.exists())

        with self.assertRaises(SandboxNotFound):
            self.service.get("sbx_" + "0" * 32)

    def test_run_sandbox_is_keyed_by_run_id(self):
        from src import agent

        first = agent.open_run_sandbox("run_d15_abc123")
        second = agent.open_run_sandbox("run_d15_abc123")

        self.assertEqual(first, second)

        agent.destroy_run_sandbox(first)

        self.assertEqual(SandboxService().get(first)["state"], "reclaimed")


if __name__ == "__main__":
    unittest.main()
