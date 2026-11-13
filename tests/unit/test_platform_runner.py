"""platform_runner：编排 coding 的平台执行通道。

用 fake ops + 临时沙箱根覆盖：成功 / 超时 / 取消 / 沙箱消失 /
无 final / 产物越界 / plan 被改（仅告警，v1.1）。
"""

from __future__ import annotations

import json
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.collab import platform_runner
from src.platform.ops import SandboxGone

SANDBOX_ID = "sbx_" + "0" * 32
EXEC_ID = "exec_test"


class _FakeOps:
    """按脚本返回 exec 状态；on_poll 钩子用来模拟 dsh 落盘。"""

    def __init__(self):
        self.detail = {"state": "succeeded", "exit_code": 0}
        self.on_poll = None
        self.submitted = None
        self.destroyed = None
        self.created = None
        self.raise_gone = False

    def create_sandbox(self, **kwargs):
        self.created = kwargs
        return {"id": SANDBOX_ID}

    def submit_exec(self, sandbox_id, **kwargs):
        self.submitted = kwargs
        return {"id": EXEC_ID}

    def exec_detail(self, exec_id):
        if self.raise_gone:
            raise SandboxGone("沙箱已回收")

        if self.on_poll:
            self.on_poll()

        return dict(self.detail)

    def cancel_exec(self, exec_id):
        self.detail = {"state": "cancelled", "exit_code": None}
        return {}, "cancelled"

    def destroy_sandbox(self, sandbox_id):
        self.destroyed = sandbox_id


class PlatformRunnerTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)

        self.run_dir = root / "run"
        (self.run_dir / "src").mkdir(parents=True)
        (self.run_dir / "plan.md").write_text("# 计划\n", encoding="utf-8")
        (self.run_dir / "review.md").write_text("VERDICT: PASS\n", encoding="utf-8")

        self.sandbox_root = root / "tier1"
        self.workspace = self.sandbox_root / SANDBOX_ID / "workspace"

        self.ops = _FakeOps()

        ops_patch = patch.object(platform_runner, "ops", self.ops)
        ops_patch.start()
        self.addCleanup(ops_patch.stop)

        root_patch = patch.object(
            platform_runner, "sandbox_root", lambda: self.sandbox_root
        )
        root_patch.start()
        self.addCleanup(root_patch.stop)

    def run_coding(self, **kwargs):
        return platform_runner.run_coding(
            "写 hello.py",
            self.run_dir,
            run_id="run-test",
            timeout=30.0,
            poll_interval=0.01,
            **kwargs,
        )

    def write_events(self, events: list[dict]) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "dsh.events.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
        )

    def test_success_copies_artifacts_back(self):
        def on_poll():
            self.write_events([
                {"type": "final",
                 "reason": {"kind": "completed"},
                 "text": "写了 hello.py",
                 "sessionId": "s1"}
            ])
            src = self.workspace / "src"
            src.mkdir(parents=True, exist_ok=True)
            (src / "hello.py").write_text("print('hi')\n", encoding="utf-8")

        self.ops.on_poll = on_poll
        result = self.run_coding(extra_env={"DEEPSEEK_API_KEY": "sk-x"})

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.text, "写了 hello.py")
        self.assertEqual((self.run_dir / "src" / "hello.py").read_text(), "print('hi')\n")
        self.assertEqual(result.sandbox_id, SANDBOX_ID)
        self.assertEqual(result.exec_id, EXEC_ID)
        self.assertEqual(result.platform_status, "succeeded")
        self.assertFalse(result.plan_tampered)
        self.assertEqual(self.ops.destroyed, SANDBOX_ID)

        # env 注入：白名单键齐、非白名单不进
        env = self.ops.submitted["env"]
        self.assertEqual(env["NO_BROWSER"], "1")
        self.assertEqual(env["DEEPSEEK_API_KEY"], "sk-x")
        self.assertNotIn("PATH", env)

        # 幂等键与 workspace_id 按计划形状
        self.assertEqual(self.ops.created["workspace_id"], "run-test")
        self.assertEqual(
            self.ops.created["idempotency_key"], "orch-run-test-1"
        )

    def test_timeout_maps_code_timeout(self):
        self.ops.detail = {"state": "timed_out", "exit_code": None}
        self.write_events([])

        result = self.run_coding()

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "code_timeout")
        # 产物不拷回
        self.assertFalse((self.run_dir / "src" / "hello.py").exists())
        # 留证：事件文件拷到 run_dir/_platform/
        self.assertTrue(
            (self.run_dir / "_platform" / "dsh.events.jsonl").is_file()
        )

    def test_cancelled_maps_code_cancelled(self):
        self.ops.detail = {"state": "cancelled", "exit_code": None}
        result = self.run_coding()

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "code_cancelled")

    def test_sandbox_gone(self):
        self.ops.raise_gone = True
        result = self.run_coding()

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "sandbox_gone")

    def test_missing_final_event(self):
        self.write_events([{"type": "session", "sessionId": "s1"}])
        result = self.run_coding()

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "reason=missing")

    def test_artifact_escape_rejected(self):
        def on_poll():
            self.write_events([
                {"type": "final", "reason": {"kind": "completed"}, "text": "x"}
            ])
            src = self.workspace / "src"
            src.mkdir(parents=True, exist_ok=True)
            (src / "evil.py").write_text("x", encoding="utf-8")

        self.ops.on_poll = on_poll

        with patch.object(
            platform_runner, "_is_reparse_point", lambda p: p.name == "evil.py"
        ):
            result = self.run_coding()

        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("artifact_escape"))
        self.assertFalse((self.run_dir / "src" / "evil.py").exists())

    def test_plan_tampered_warns_only(self):
        def on_poll():
            self.write_events([
                {"type": "final", "reason": {"kind": "completed"}, "text": "x"}
            ])
            (self.workspace / "plan.md").write_text("# 被改\n", encoding="utf-8")
            src = self.workspace / "src"
            src.mkdir(parents=True, exist_ok=True)
            (src / "a.py").write_text("x", encoding="utf-8")

        self.ops.on_poll = on_poll
        result = self.run_coding()

        # v1.1：只告警不拦截
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.plan_tampered)
        self.assertTrue((self.run_dir / "src" / "a.py").is_file())

    def test_deadline_exceeded_cancels(self):
        self.ops.detail = {"state": "running", "exit_code": None}

        result = platform_runner.run_coding(
            "写 hello.py",
            self.run_dir,
            run_id="run-test",
            timeout=0.05,
            poll_interval=0.01,
            deadline_grace=0.05,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "code_timeout")


if __name__ == "__main__":
    unittest.main()
