"""编排-平台接线集成测试：真实 tier1 跑一条短命令 + 注入 env + 输出落盘。

不联网、不跑 dsh：用 python 自身验证「沙箱工作区里执行、白名单 env
注入、产物文件可读」这条链路（dsh 的联网验证由 spike 与真机联调覆盖）。
"""

from __future__ import annotations

import sys
import time
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.platform import ops
from src.platform.paths import sandbox_root
from tests.helper import isolate_paths, wait_for_exec_worker

TERMINAL = {"succeeded", "failed", "cancelled", "timed_out"}


class OrchPlatformExecTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)

        root = patch.dict(
            "os.environ",
            {
                "PERSONAL_AGENT_SANDBOX_ROOT": str(
                    Path(self.temp.name) / "sandboxes"
                )
            },
            clear=False,
        )
        root.start()
        self.addCleanup(root.stop)

    def test_short_command_with_env_and_file_output(self):
        sandbox = ops.create_sandbox(
            idempotency_key="orch-platform-it",
            workspace_id="run-it",
            ttl_seconds=600,
        )
        sandbox_id = sandbox["id"]

        command = [
            sys.executable,
            "-c",
            "import os, pathlib;"
            "pathlib.Path('marker.txt').write_text("
            "os.environ.get('NO_BROWSER', ''), encoding='utf-8');"
            "print('stdout-ok')",
        ]

        row = ops.submit_exec(
            sandbox_id,
            command=command,
            timeout_ms=60_000,
            max_output_bytes=65_536,
            run_id="run-it",
            step_id="coding",
            env={"NO_BROWSER": "1", "DEEPSEEK_API_KEY": "sk-it-secret"},
        )
        exec_id = row["id"]

        deadline = time.monotonic() + 90
        detail = row

        while time.monotonic() < deadline:
            detail = ops.exec_detail(exec_id)

            if detail["state"] in TERMINAL:
                break

            time.sleep(0.5)

        self.addCleanup(wait_for_exec_worker, exec_id)

        self.assertEqual(detail["state"], "succeeded", detail.get("stderr"))
        self.assertEqual(detail["exit_code"], 0)
        self.assertIn("stdout-ok", detail["stdout"])

        # 产物落在沙箱工作区，内容是注入的 env 值
        marker = (
            sandbox_root() / sandbox_id / "workspace" / "marker.txt"
        )
        self.assertTrue(marker.is_file())
        self.assertEqual(marker.read_text(encoding="utf-8"), "1")

        # env 的值不落库（键名出现在命令文本里是业务代码自身的引用）
        self.assertNotIn("sk-it-secret", detail["argv_json"])

        ops.destroy_sandbox(sandbox_id)


if __name__ == "__main__":
    unittest.main()
