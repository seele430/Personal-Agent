"""D21 每租户并发配额与节点容量：超额 429 CAPACITY_EXCEEDED，回收释放名额。"""

from __future__ import annotations

import unittest

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.main import app
from src.platform.sandbox_service import (
    DEFAULT_QUOTA,
    MAX_QUOTA,
    QuotaExceeded,
    SandboxService,
    sandbox_quota,
)
from tests.helper import isolate_paths


class QuotaApiTest(unittest.TestCase):
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
                "PERSONAL_AGENT_SANDBOX_QUOTA": "2",
            },
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def create(self, key: str, *, tenant: str | None = None):
        headers = {"Idempotency-Key": key}

        if tenant is not None:
            headers["X-Tenant-Id"] = tenant

        return self.client.post(
            "/v1/sandboxes",
            json={
                "tier": "tier1",
                "workspace_id": f"ws_{key}",
                "ttl_seconds": 600,
            },
            headers=headers,
        )

    def test_quota_blocks_third_sandbox_then_releases(self):
        first = self.create("quota-1")
        second = self.create("quota-2")

        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(second.status_code, 201, second.text)

        blocked = self.create("quota-3")

        self.assertEqual(blocked.status_code, 429, blocked.text)
        self.assertEqual(
            blocked.json()["error"]["code"], "CAPACITY_EXCEEDED"
        )
        self.assertTrue(blocked.json()["error"]["retryable"])

        # 回收后名额释放
        self.client.post(
            f"/v1/sandboxes/{first.json()['sandbox_id']}/destroy"
        )

        allowed = self.create("quota-3")
        self.assertEqual(allowed.status_code, 201, allowed.text)

    def test_idempotent_replay_does_not_consume_quota(self):
        with patch.dict(
            "os.environ", {"PERSONAL_AGENT_SANDBOX_QUOTA": "1"}
        ):
            first = self.create("replay-1")
            replay = self.create("replay-1")
            other = self.create("replay-2")

        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(
            replay.json()["sandbox_id"], first.json()["sandbox_id"]
        )
        self.assertEqual(other.status_code, 429, other.text)

    def test_node_capacity_is_enforced(self):
        # 注册一个容量为 1 的默认节点
        self.client.post(
            "/v1/nodes",
            json={"node_id": "node_local", "capacity": 1},
        )

        first = self.create("cap-1")
        second = self.create("cap-2")

        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(second.status_code, 429, second.text)
        self.assertEqual(
            second.json()["error"]["code"], "CAPACITY_EXCEEDED"
        )

    def test_quota_env_fallbacks(self):
        for raw in ("", "abc", "0", "-3"):
            with self.subTest(raw=raw):
                with patch.dict(
                    "os.environ", {"PERSONAL_AGENT_SANDBOX_QUOTA": raw}
                ):
                    self.assertEqual(sandbox_quota(), DEFAULT_QUOTA)

        with patch.dict(
            "os.environ",
            {"PERSONAL_AGENT_SANDBOX_QUOTA": str(MAX_QUOTA + 100)},
        ):
            self.assertEqual(sandbox_quota(), MAX_QUOTA)


class QuotaServiceTest(unittest.TestCase):
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
                "PERSONAL_AGENT_SANDBOX_QUOTA": "2",
            },
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.service = SandboxService(
            Path(self.temp.name) / "platform.db",
            Path(self.temp.name) / "sandboxes",
        )

    def test_concurrent_creates_cannot_exceed_quota(self):
        def create(index: int) -> str:
            try:
                self.service.create(
                    idempotency_key=f"race-{index}",
                    workspace_id=f"ws_race_{index}",
                )
                return "created"
            except QuotaExceeded:
                return "quota"

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(create, range(10)))

        self.assertEqual(results.count("created"), 2)
        self.assertEqual(results.count("quota"), 8)
        self.assertEqual(self.service.count_for_tenant(), 2)

    def test_tenants_have_independent_quotas(self):
        for index in range(2):
            self.service.create(
                idempotency_key=f"a-{index}",
                workspace_id=f"ws_a_{index}",
                tenant_id="tenant_a",
            )

        # 另一个租户不受影响
        other = self.service.create(
            idempotency_key="b-0",
            workspace_id="ws_b_0",
            tenant_id="tenant_b",
        )

        self.assertEqual(other["tenant_id"], "tenant_b")
        self.assertEqual(self.service.count_for_tenant("tenant_a"), 2)
        self.assertEqual(self.service.count_for_tenant("tenant_b"), 1)

        with self.assertRaises(QuotaExceeded):
            self.service.create(
                idempotency_key="a-3",
                workspace_id="ws_a_3",
                tenant_id="tenant_a",
            )


if __name__ == "__main__":
    unittest.main()
