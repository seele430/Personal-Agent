"""D22 启动对账：中断的 exec 落 failed、孤儿沙箱收敛、孤儿目录只清自己的。"""

from __future__ import annotations

import os
import shutil
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.core.run_control import CancellationToken
from src.platform import active_execs
from src.platform.exec_store import ExecStore
from src.platform.reconcile import reconcile_startup
from src.platform.sandbox_service import SandboxService
from tests.helper import isolate_paths


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)

        self.root = Path(self.temp.name) / "sandboxes"

        patcher = patch.dict(
            "os.environ",
            {
                "PERSONAL_AGENT_SANDBOX_ROOT": str(self.root),
                "PERSONAL_AGENT_PLATFORM_REAPER": "0",
            },
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.service = SandboxService()
        self.store = ExecStore()

    # ------------------------------------------------------------------

    def make(self, key: str = "reconcile-key") -> str:
        return self.service.create(idempotency_key=key)["id"]

    def workspace(self, sandbox_id: str) -> Path:
        return self.root / sandbox_id / "workspace"

    # ------------------------------------------------------------------
    # 中断的 exec
    # ------------------------------------------------------------------

    def test_interrupted_exec_becomes_failed(self):
        sandbox_id = self.make()
        self.service.begin_exec(sandbox_id)

        row = self.store.create(
            sandbox_id,
            argv=["python", "-c", "print(1)"],
            run_id=None,
            step_id=None,
            timeout_ms=1000,
            max_output_bytes=1024,
        )

        summary = reconcile_startup()

        self.assertEqual(summary["execs_failed"], 1)
        self.assertEqual(summary["sandboxes_idle"], 1)

        exec_row = self.store.get(row["id"])
        self.assertEqual(exec_row["state"], "failed")
        self.assertEqual(exec_row["error_code"], "INTERRUPTED_BY_RESTART")
        self.assertEqual(exec_row["result_code"], "EXEC_FAILED")

        self.assertEqual(self.service.get(sandbox_id)["state"], "idle")

    def test_active_exec_is_not_touched(self):
        sandbox_id = self.make()
        self.service.begin_exec(sandbox_id)

        row = self.store.create(
            sandbox_id,
            argv=["python", "-c", "print(1)"],
            run_id=None,
            step_id=None,
            timeout_ms=1000,
            max_output_bytes=1024,
        )

        token = CancellationToken()
        active_execs.register(row["id"], sandbox_id, token)
        self.addCleanup(active_execs.unregister, row["id"])

        summary = reconcile_startup()

        self.assertEqual(summary["execs_failed"], 0)
        self.assertEqual(self.store.get(row["id"])["state"], "queued")

    # ------------------------------------------------------------------
    # 沙箱
    # ------------------------------------------------------------------

    def test_running_sandbox_without_exec_returns_to_idle(self):
        sandbox_id = self.make()
        self.service.begin_exec(sandbox_id)

        summary = reconcile_startup()

        self.assertEqual(summary["sandboxes_idle"], 1)
        self.assertEqual(self.service.get(sandbox_id)["state"], "idle")

    def test_missing_workspace_marks_sandbox_failed(self):
        sandbox_id = self.make()
        shutil.rmtree(self.workspace(sandbox_id))

        summary = reconcile_startup()

        self.assertEqual(summary["sandboxes_failed"], 1)

        row = self.service.get(sandbox_id)
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["fail_reason"], "workspace_missing")

    def test_reclaimed_sandbox_is_ignored(self):
        sandbox_id = self.make()
        self.service.reclaim(sandbox_id)

        summary = reconcile_startup()

        self.assertEqual(summary["sandboxes_failed"], 0)
        self.assertEqual(self.service.get(sandbox_id)["state"], "reclaimed")

    # ------------------------------------------------------------------
    # 目录
    # ------------------------------------------------------------------

    def test_orphan_dirs_removed_but_foreign_dirs_kept(self):
        kept = self.make(key="kept-key")

        orphan = self.root / ("sbx_" + "a" * 32)
        orphan.mkdir(parents=True)

        foreign = self.root / "keepme"
        foreign.mkdir()

        summary = reconcile_startup()

        self.assertEqual(summary["orphan_dirs_removed"], 1)
        self.assertFalse(orphan.exists())
        self.assertTrue(foreign.exists())
        self.assertTrue(self.workspace(kept).is_dir())

    def test_reconcile_is_idempotent(self):
        sandbox_id = self.make()
        self.service.begin_exec(sandbox_id)
        shutil.rmtree(self.workspace(sandbox_id))

        first = reconcile_startup()
        second = reconcile_startup()

        self.assertGreaterEqual(first["sandboxes_failed"], 1)
        self.assertEqual(
            second,
            {
                "execs_failed": 0,
                "sandboxes_idle": 0,
                "sandboxes_failed": 0,
                "orphan_dirs_removed": 0,
            },
        )


if __name__ == "__main__":
    unittest.main()
