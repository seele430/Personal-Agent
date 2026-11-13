"""命令 exec 的受控 env 注入：

- 非白名单键一律拒绝（宿主侧 ValueError → ops 转 InvalidArgument）；
- 注入值只随内存队列项 / stdin 载荷传递：platform_execs.argv_json 不含值；
- 子进程能读到注入值（真跑一次 tier1 runner）；
- runner 侧独立再校验，绕过宿主构造的非法 spec 同样被拒。
"""

from __future__ import annotations

import sys
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.platform import ops
from src.platform import tier1_runner
from src.platform.env_injection import (
    ENV_INJECTION_ALLOWLIST,
    validate_env_injection,
)
from src.platform.exec_service import Tier1Executor
from src.platform.exec_store import ExecStore
from src.platform.sandbox_service import InvalidArgument
from tests.helper import isolate_paths


class ValidateEnvInjectionTest(unittest.TestCase):
    def test_allowlist_keys_accepted(self):
        env = {key: "v" for key in ENV_INJECTION_ALLOWLIST}
        self.assertEqual(validate_env_injection(env), env)

    def test_non_allowlist_key_rejected(self):
        with self.assertRaises(ValueError):
            validate_env_injection({"PATH": "C:\\evil"})

        with self.assertRaises(ValueError):
            validate_env_injection({"OPENAI_API_KEY": "x"})

    def test_bad_values_rejected(self):
        for bad in ("", 123, "a\x00b"):
            with self.assertRaises(ValueError):
                validate_env_injection({"NO_BROWSER": bad})

    def test_none_and_empty(self):
        self.assertEqual(validate_env_injection(None), {})
        self.assertEqual(validate_env_injection({}), {})

    def test_runner_revalidates(self):
        with self.assertRaises(tier1_runner.InvalidSpec):
            tier1_runner.run_command(
                {"argv": ["python", "-V"], "env": {"PATH": "C:\\evil"}}
            )


class SubmitExecEnvTest(unittest.TestCase):
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

        queue = patch("src.platform.ops.exec_queue", lambda: _FakeQueue())
        queue.start()
        self.addCleanup(queue.stop)

        self.sandbox_id = ops.create_sandbox(
            idempotency_key="env-test", ttl_seconds=600
        )["id"]

    def test_non_allowlist_key_rejected(self):
        with self.assertRaises(InvalidArgument):
            ops.submit_exec(
                self.sandbox_id,
                command=["python", "-V"],
                timeout_ms=1000,
                max_output_bytes=1024,
                env={"HTTP_PROXY": "http://evil"},
            )

    def test_env_not_persisted_in_argv(self):
        row = ops.submit_exec(
            self.sandbox_id,
            command=["python", "-V"],
            timeout_ms=1000,
            max_output_bytes=1024,
            env={"DEEPSEEK_API_KEY": "sk-test-secret"},
        )

        stored = ExecStore().get(row["id"])
        self.assertNotIn("sk-test-secret", stored["argv_json"])
        self.assertNotIn("DEEPSEEK_API_KEY", stored["argv_json"])


class Tier1EnvInjectionTest(unittest.TestCase):
    """真跑一条命令：子进程必须能读到注入值。"""

    def test_child_reads_injected_env(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            outcome = Tier1Executor(root=temp).execute_command(
                [
                    sys.executable,
                    "-c",
                    "import os; print(os.environ.get('NO_BROWSER', ''))",
                ],
                timeout_ms=30_000,
                max_output_bytes=65_536,
                env={"NO_BROWSER": "1"},
            )

        self.assertEqual(outcome.status, "succeeded", outcome.stderr)
        self.assertEqual(outcome.stdout.strip(), "1")

    def test_executor_rejects_bad_key(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            with self.assertRaises(ValueError):
                Tier1Executor(root=temp).execute_command(
                    ["python", "-V"], env={"PATH": "x"}
                )


class _FakeQueue:
    def accepting(self) -> bool:
        return True

    def submit(self, item) -> str:
        return "ok"


if __name__ == "__main__":
    unittest.main()
