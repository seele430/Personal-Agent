"""D26 审计与限流：拒绝留痕、关键操作留痕、超额 429。"""

from __future__ import annotations

import asyncio
import sys
import unittest

from contextlib import closing

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from src.main import _platform_actor_key, _platform_auth, app
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
        # 未认证方的 actor 必须落在 anon 桶，不能和服务端令牌的指纹混在一起，
        # 否则审计里分不出「谁在被拒」。
        self.assertTrue(records[0]["actor"].startswith("anon:"))
        self.assertFalse(records[0]["detail"]["auth_ok"])

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

    def test_audit_is_not_rate_limited(self):
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
            audit = self.client.get("/v1/audit", headers=AUTH)

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 429, second.text)
        # 事故排查时审计不能被自己的限流挡住
        self.assertEqual(audit.status_code, 200, audit.text)

    def test_audit_exemption_requires_auth(self):
        with patch.dict(
            "os.environ",
            {
                "PERSONAL_AGENT_PLATFORM_RATE_PER_MIN": "1",
                "PERSONAL_AGENT_PLATFORM_RATE_BURST": "1",
            },
        ):
            rate_limit.reset_limiter()

            first = self.client.get("/v1/audit")
            second = self.client.get("/v1/audit")

        self.assertEqual(first.status_code, 401, first.text)
        # 匿名不能靠 /v1/audit 的免限流豁免无限探测
        self.assertEqual(second.status_code, 429, second.text)

    def test_audit_filters(self):
        self.create()
        self.client.get("/v1/nodes")  # 无令牌 → auth.denied

        creates = self.client.get(
            "/v1/audit?event=sandbox.create", headers=AUTH
        ).json()["records"]
        denied = self.client.get(
            "/v1/audit?outcome=denied", headers=AUTH
        ).json()["records"]
        future = self.client.get(
            "/v1/audit?since=2999-01-01T00:00:00%2B00:00", headers=AUTH
        ).json()["records"]

        self.assertTrue(creates)
        self.assertTrue(all(item["event"] == "sandbox.create" for item in creates))
        self.assertTrue(denied)
        self.assertTrue(all(item["outcome"] == "denied" for item in denied))
        self.assertEqual(future, [])

        self.assertEqual(
            self.client.get("/v1/audit?event=nope", headers=AUTH).status_code,
            400,
        )
        self.assertEqual(
            self.client.get("/v1/audit?since=yesterday", headers=AUTH).status_code,
            400,
        )

    def test_prune_removes_records_older_than_retention(self):
        from src.platform.audit import AuditLog

        log = AuditLog()
        log.record("auth.denied", outcome="denied", code="UNAUTHORIZED")
        old_id = log.list(limit=1)[0]["id"]
        log.record("auth.denied", outcome="denied", code="UNAUTHORIZED")

        with closing(log.connect()) as conn, conn:
            conn.execute(
                "UPDATE agent_audit SET at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (old_id,),
            )

        self.assertEqual(log.prune(30), 1)
        self.assertEqual(len(log.list(limit=10)), 1)
        self.assertEqual(log.prune(0), 0)  # 0 = 永久保留


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

    # ------------------------------------------------------------------
    # 分桶隔离：匿名流量不得消耗合法令牌的桶
    # ------------------------------------------------------------------

    def test_anonymous_traffic_does_not_drain_token_bucket(self):
        with patch.dict(
            "os.environ",
            {
                "PERSONAL_AGENT_PLATFORM_RATE_PER_MIN": "1",
                "PERSONAL_AGENT_PLATFORM_RATE_BURST": "5",
            },
        ):
            rate_limit.reset_limiter()

            for _ in range(10):
                self.client.get("/v1/nodes")  # 无令牌：只该消耗 anon 桶

            first = self.client.get("/v1/nodes", headers=AUTH)
            second = self.client.get("/v1/nodes", headers=AUTH)

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)

    def test_anonymous_denials_are_audited_under_anon_actor(self):
        with patch.dict(
            "os.environ",
            {
                "PERSONAL_AGENT_PLATFORM_RATE_PER_MIN": "1",
                "PERSONAL_AGENT_PLATFORM_RATE_BURST": "1",
            },
        ):
            rate_limit.reset_limiter()

            for _ in range(3):
                self.client.get("/v1/nodes")

        actors = {
            record["actor"]
            for record in AuditLog().list(limit=50)
            if record["event"] in {"auth.denied", "rate_limited"}
        }

        self.assertTrue(actors)
        self.assertTrue(
            all(actor.startswith("anon:") for actor in actors),
            actors,
        )

    def test_malformed_authorization_is_401_not_500(self):
        with patch.dict(
            "os.environ",
            {"PERSONAL_AGENT_PLATFORM_RATE_PER_MIN": "0"},
        ):
            rate_limit.reset_limiter()

            cases = [
                {},
                {"Authorization": ""},
                {"Authorization": "Bearer"},
                {"Authorization": "Bearer "},
                {"Authorization": "Basic abc"},
                {"Authorization": "Bearer " + "x" * 8192},
            ]

            for headers in cases:
                response = self.client.get("/v1/nodes", headers=headers)
                self.assertEqual(
                    response.status_code,
                    401,
                    (headers, response.text),
                )


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeURL:
    def __init__(self, path):
        self.path = path


class _FakeRequest:
    """只带中间件会读到的字段：headers / client / url。"""

    def __init__(self, headers=None, host="10.0.0.1", path="/v1/nodes"):
        self.headers = headers or {}
        self.client = _FakeClient(host)
        self.url = _FakeURL(path)


def _fake_request(headers=None, host="10.0.0.1", path="/v1/nodes"):
    return _FakeRequest(headers=headers, host=host, path=path)


async def _call_next_ok(request):
    return JSONResponse(status_code=200, content={"ok": True})


class ActorKeyTest(unittest.TestCase):
    """键函数与认证中间件的直接调用：不经过 HTTP 栈。"""

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)
        rate_limit.reset_limiter()
        self.addCleanup(rate_limit.reset_limiter)

        patcher = patch.dict(
            "os.environ",
            {
                "PERSONAL_AGENT_PLATFORM_TOKEN": TOKEN,
                "PERSONAL_AGENT_PLATFORM_RATE_PER_MIN": "0",
            },
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_distinct_hosts_get_distinct_anonymous_keys(self):
        first = _platform_actor_key(_fake_request(host="1.1.1.1"))
        second = _platform_actor_key(_fake_request(host="2.2.2.2"))

        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("anon:"), first)
        self.assertTrue(second.startswith("anon:"), second)

    def test_valid_token_uses_token_key(self):
        request = _fake_request(headers={"authorization": f"Bearer {TOKEN}"})

        self.assertTrue(_platform_actor_key(request).startswith("tok:"))

    def test_non_ascii_authorization_is_401_not_500(self):
        request = _fake_request(headers={"authorization": "Bearer 令牌"})

        response = asyncio.run(_platform_auth(request, _call_next_ok))

        self.assertEqual(response.status_code, 401)


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

    def test_idle_full_buckets_are_reclaimed(self):
        now = [0.0]
        limiter = rate_limit.RateLimiter(
            rate_per_minute=60,
            burst=1,
            clock=lambda: now[0],
            max_buckets=10,
            idle_ttl_seconds=5.0,
        )

        for index in range(50):
            limiter.allow(f"k{index}")
            now[0] += 10.0  # 每个桶都会补回满

        self.assertLessEqual(len(limiter._buckets), 11)

    def test_drained_bucket_is_never_reclaimed(self):
        now = [0.0]
        limiter = rate_limit.RateLimiter(
            rate_per_minute=1,
            burst=1,
            clock=lambda: now[0],
            max_buckets=2,
            idle_ttl_seconds=0.1,
        )

        self.assertTrue(limiter.allow("victim"))

        for index in range(20):
            limiter.allow(f"other{index}")
            now[0] += 1.0

        # 被打空的桶必须留着：回收它等于把额度还给了攻击者。
        self.assertIn("victim", limiter._buckets)
        self.assertFalse(limiter.allow("victim"))



if __name__ == "__main__":
    unittest.main()