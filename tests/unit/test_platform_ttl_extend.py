"""长任务 TTL 续期：

- extend_ttl 把 expires_at 推迟到 now+seconds；只延后不提前；
- 非法值（0 / 负数 / 超上限 / 非 int）与已回收沙箱一律拒绝；
- submit_exec 保证 剩余 TTL >= timeout + 120 s，不足自动续；
- 续期后 reaper 的到期查询不再命中该沙箱。
"""

from __future__ import annotations

import unittest

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.platform import ops
from src.platform.sandbox_service import (
    MAX_TTL_SECONDS,
    InvalidArgument,
    InvalidTransition,
    SandboxService,
)
from tests.helper import isolate_paths


class _FakeQueue:
    def accepting(self) -> bool:
        return True

    def submit(self, item) -> str:
        return "ok"


class ExtendTtlTest(unittest.TestCase):
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

        self.service = SandboxService()
        self.sandbox_id = ops.create_sandbox(
            idempotency_key="ttl-test", ttl_seconds=600
        )["id"]

    def expires_at(self) -> datetime:
        return datetime.fromisoformat(self.service.get(self.sandbox_id)["expires_at"])

    def test_extend_moves_expiry_forward(self):
        before = self.expires_at()
        self.service.extend_ttl(self.sandbox_id, 3600)
        after = self.expires_at()

        self.assertGreater(after, before)
        remaining = (after - datetime.now(timezone.utc)).total_seconds()
        self.assertGreater(remaining, 3500)
        self.assertLessEqual(remaining, 3600)

    def test_shorter_extension_is_noop(self):
        self.service.extend_ttl(self.sandbox_id, 3600)
        before = self.expires_at()
        self.service.extend_ttl(self.sandbox_id, 300)
        self.assertEqual(self.expires_at(), before)

    def test_invalid_values_rejected(self):
        for bad in (0, -5, MAX_TTL_SECONDS + 1, 1.5, "600"):
            with self.assertRaises((InvalidArgument, TypeError)):
                self.service.extend_ttl(self.sandbox_id, bad)  # type: ignore[arg-type]

    def test_reclaimed_sandbox_rejected(self):
        self.service.reclaim(self.sandbox_id, reason="manual")

        with self.assertRaises(InvalidTransition):
            self.service.extend_ttl(self.sandbox_id, 3600)

    def test_submit_exec_extends_ttl_for_long_timeout(self):
        queue = patch("src.platform.ops.exec_queue", lambda: _FakeQueue())
        queue.start()
        self.addCleanup(queue.stop)

        with patch.dict(
            "os.environ",
            {"PERSONAL_AGENT_EXEC_MAX_TIMEOUT_MS": "3600000"},
            clear=False,
        ):
            ops.submit_exec(
                self.sandbox_id,
                command=["python", "-c", "print(1)"],
                timeout_ms=1_800_000,
                max_output_bytes=65_536,
                extend_ttl=True,
            )

        remaining = (
            self.expires_at() - datetime.now(timezone.utc)
        ).total_seconds()
        # 1800 s 超时 + 120 s 余量
        self.assertGreater(remaining, 1800 + 100)

    def test_short_exec_does_not_extend(self):
        queue = patch("src.platform.ops.exec_queue", lambda: _FakeQueue())
        queue.start()
        self.addCleanup(queue.stop)

        before = self.expires_at()
        ops.submit_exec(
            self.sandbox_id,
            command=["python", "-c", "print(1)"],
            timeout_ms=30_000,
            max_output_bytes=65_536,
        )
        self.assertEqual(self.expires_at(), before)


if __name__ == "__main__":
    unittest.main()
