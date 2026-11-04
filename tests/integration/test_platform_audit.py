"""D26 审计与限流：拒绝留痕、关键操作留痕、超额 429。"""

from __future__ import annotations

import sys
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.main import app
from src.platform import rate_limit
from src.platform.audit import AuditLog
from tests.helper import isolate_paths, wait_for_exec_worker

TOKEN = "d26-audit-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)
        rate_limit.reset_limiter()
        self.addCleanup(rate_limit.reset_limiter)

        patcher = patch.dict(
            "os.environ",
            {
                "PERSONAL_AGENT_SANDBOX_ROOT": str(
                    Path(self.temp.name) / "sandboxes"
                ),
                "PERSONAL_AGENT_PLATFORM_REAPER": "0",
                "PERSONAL_AGENT_PLATFORM_TOKEN": TOKEN,
            },
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    # ------------------------------------------------------------------

    def create(self, headers=None):
        return self.client.post(
            "/v1/sandboxes",
            json={
                "tier": "tier1",
                "workspace_id": "ws_audit",
                "ttl_seconds": 600,
            },
            headers={
                "Idempotency-Key": "audit-key",
                **(headers or AUTH),
            },
        )

    def events(self) -> list[str]:
        return [item["event"] for item in AuditLog().list(limit=50)]

    # ------------------------------------------------------------------
    # 拒绝留痕
    # ------------------------------------------------------------------

    def test_auth_denial_is_audited(self):
        response = self.client.get("/v1/nodes")

        self.assertEqual(response.status_code, 401)

        records = AuditLog().list(limit=10)
        self.assertEqual(records[0]["event"], "auth.denied")
        self.assertEqual(records[0]["outcome"], "denied")
        self.assertEqual(records[0]["code"], "UNAUTHORIZED")
        self.assertTrue(records[0]["actor"].startswith("tok:"))

    # ------------------------------------------------------------------
    # 操作留痕
    # ------------------------------------------------------------------

    def test_sandbox_and_exec_lifecycle_is_audited(self):
        created = self.create()
        self.assertEqual(created.status_code, 201, created.text)
        sandbox_id = created.json()["sandbox_id"]

        accepted = self.client.post(
            f"/v1/sandboxes/{sandbox_id}/exec",
            json={
                "command": [sys.executable, "-c", "import time; time.sleep(30)"],
                "timeout_ms": 30000,
            },
            headers=AUTH,
        )
        self.assertEqual(accepted.status_code, 202, accepted.text)
        exec_id = accepted.json()["exec_id"]

        cancelled = self.client.post(
            f"/v1/exec/{exec_id}/cancel", headers=AUTH
        )
        self.assertEqual(cancelled.status_code, 202, cancelled.text)
        wait_for_exec_worker(exec_id)

        destroyed = self.client.post(
            f"/v1/sandboxes/{sandbox_id}/destroy", headers=AUTH
        )
        self.assertEqual(destroyed.status_code, 200, destroyed.text)

        events = self.events()
        self.assertIn("sandbox.create", events)
        self.assertIn("exec.create", events)
        self.assertIn("exec.cancel", events)
        self.assertIn("sandbox.destroy", events)

        records = AuditLog().list(limit=20)
        creates = [
            item for item in records if item["event"] == "sandbox.create"
        ]
        self.assertEqual(creates[0]["sandbox_id"], sandbox_id)
        execs = [item for item in records if item["event"] == "exec.create"]
        self.assertEqual(execs[0]["exec_id"], exec_id)

    def test_audit_endpoint_lists_records(self):
        self.create()

        response = self.client.get("/v1/audit?limit=5", headers=AUTH)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["records"]), 1)
        self.assertEqual(
            response.json()["records"][0]["event"], "sandbox.create"
        )

    def test_audit_endpoint_requires_token(self):
        response = self.client.get("/v1/audit")

        self.assertEqual(response.status_code, 401)

    # ------------------------------------------------------------------
    # 限流
    # ------------------------------------------------------------------

    def test_rate_limit_returns_429_and_is_audited(self):
        with patch.dict(
            "os.environ",
            {
                "PERSONAL_AGENT_PLATFORM_RATE_PER_MIN": "1",
                "PERSONAL_AGENT_PLATFORM_RATE_BURST": "1",
            },
        ):
            rate_limit.reset_limiter()

            first = self.client.get("/v1/nodes", headers=AUTH)
            second = self.client.get("/v1/nodes", headers=AUTH)

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 429, second.text)
        self.assertEqual(second.json()["error"]["code"], "RATE_LIMITED")
        self.assertTrue(second.json()["error"]["retryable"])
        self.assertIn("retry-after", second.headers)

        events = self.events()
        self.assertIn("rate_limited", events)

    def test_health_is_not_rate_limited(self):
        with patch.dict(
            "os.environ",
            {
                "PERSONAL_AGENT_PLATFORM_RATE_PER_MIN": "1",
                "PERSONAL_AGENT_PLATFORM_RATE_BURST": "1",
            },
        ):
            rate_limit.reset_limiter()

            for _ in range(3):
                self.assertEqual(self.client.get("/health").status_code, 200)


class RateLimiterUnitTest(unittest.TestCase):
    def test_bucket_refills_with_injected_clock(self):
        now = [0.0]
        limiter = rate_limit.RateLimiter(
            rate_per_minute=60, burst=1, clock=lambda: now[0]
        )

        self.assertTrue(limiter.allow("k"))
        self.assertFalse(limiter.allow("k"))

        now[0] = 1.1  # 60/分钟 = 每秒 1 个
        self.assertTrue(limiter.allow("k"))

    def test_retry_after_is_positive(self):
        limiter = rate_limit.RateLimiter(
            rate_per_minute=60, burst=1, clock=lambda: 0.0
        )
        limiter.allow("k")

        self.assertGreaterEqual(limiter.retry_after_seconds("k"), 1)


if __name__ == "__main__":
    unittest.main()
