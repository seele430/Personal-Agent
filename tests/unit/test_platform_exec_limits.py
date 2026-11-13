"""exec 限额可配置（编排 coding 需要 1800 s）：

- 默认值不变：300 s / 1 MiB；
- `PERSONAL_AGENT_EXEC_MAX_TIMEOUT_MS` / `PERSONAL_AGENT_EXEC_MAX_OUTPUT_BYTES`
  只放宽不收紧；
- 绝对上限 1 h / 4 MiB，env 超过上限按上限截断；非法值回落默认。
"""

from __future__ import annotations

import unittest

from tempfile import TemporaryDirectory
from unittest.mock import patch

from pathlib import Path

from src.platform import exec_service
from src.platform import ops
from src.platform.sandbox_service import InvalidArgument
from tests.helper import isolate_paths

ENV_TIMEOUT = "PERSONAL_AGENT_EXEC_MAX_TIMEOUT_MS"
ENV_OUTPUT = "PERSONAL_AGENT_EXEC_MAX_OUTPUT_BYTES"


class _FakeQueue:
    """只验证 submit_exec 的校验逻辑，不真正启动执行器。"""

    def accepting(self) -> bool:
        return True

    def submit(self, item) -> str:
        return "ok"


class ExecLimitEnvTest(unittest.TestCase):
    def test_defaults_unchanged(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(exec_service.max_timeout_ms(), 300_000)
            self.assertEqual(exec_service.max_output_bytes_limit(), 1_048_576)

    def test_env_override_relaxes(self):
        with patch.dict(
            "os.environ",
            {ENV_TIMEOUT: "1800000", ENV_OUTPUT: "2097152"},
            clear=True,
        ):
            self.assertEqual(exec_service.max_timeout_ms(), 1_800_000)
            self.assertEqual(exec_service.max_output_bytes_limit(), 2_097_152)

    def test_env_above_ceiling_clamped(self):
        with patch.dict(
            "os.environ",
            {ENV_TIMEOUT: "99999999", ENV_OUTPUT: "99999999"},
            clear=True,
        ):
            self.assertEqual(exec_service.max_timeout_ms(), 3_600_000)
            self.assertEqual(exec_service.max_output_bytes_limit(), 4_194_304)

    def test_invalid_env_falls_back(self):
        for bad in ("abc", "-5", "0"):
            with patch.dict(
                "os.environ", {ENV_TIMEOUT: bad, ENV_OUTPUT: bad}, clear=True
            ):
                self.assertEqual(exec_service.max_timeout_ms(), 300_000)
                self.assertEqual(exec_service.max_output_bytes_limit(), 1_048_576)


class SubmitExecLimitTest(unittest.TestCase):
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
            idempotency_key="limits-test", ttl_seconds=600
        )["id"]

    def submit(self, timeout_ms: int, max_output_bytes: int = 65_536):
        return ops.submit_exec(
            self.sandbox_id,
            command=["python", "-c", "print(1)"],
            timeout_ms=timeout_ms,
            max_output_bytes=max_output_bytes,
        )

    def test_over_300s_rejected_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(InvalidArgument):
                self.submit(timeout_ms=300_001)

    def test_over_300s_accepted_with_env(self):
        with patch.dict(
            "os.environ", {ENV_TIMEOUT: "3600000"}, clear=True
        ):
            row = self.submit(timeout_ms=1_800_000)
            self.assertEqual(row["timeout_ms"], 1_800_000)

    def test_over_ceiling_rejected_even_with_env(self):
        with patch.dict(
            "os.environ", {ENV_TIMEOUT: "99999999"}, clear=True
        ):
            with self.assertRaises(InvalidArgument):
                self.submit(timeout_ms=3_600_001)

    def test_output_limit_follows_env(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(InvalidArgument):
                self.submit(timeout_ms=1000, max_output_bytes=1_048_577)

        with patch.dict(
            "os.environ", {ENV_OUTPUT: "4194304"}, clear=True
        ):
            row = self.submit(timeout_ms=1000, max_output_bytes=2_097_152)
            self.assertEqual(row["max_output_bytes"], 2_097_152)

            with self.assertRaises(InvalidArgument):
                self.submit(timeout_ms=1000, max_output_bytes=4_194_305)


if __name__ == "__main__":
    unittest.main()
