"""D16：TTL 回收、tombstone 保留与 410 语义。

时间不靠 sleep 硬等：``sweep_once(now=...)`` 接受注入的时钟；只有「惰性过期」
一条用真实短 TTL（1 秒）验证 API 访问时的兜底回收。
"""

from __future__ import annotations

import time
import unittest

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.main import app
from src.platform import reaper
from src.platform.sandbox_service import SandboxService
from tests.helper import isolate_paths

PYTHON = __import__("sys").executable


class PlatformTtlTest(unittest.TestCase):
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
            },
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    # ------------------------------------------------------------------

    def create(self, key: str = "ttl-key", *, ttl_seconds: int = 600):
        response = self.client.post(
            "/v1/sandboxes",
            json={
                "tier": "tier1",
                "workspace_id": "ws_ttl",
                "ttl_seconds": ttl_seconds,
            },
            headers={"Idempotency-Key": key},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    # ------------------------------------------------------------------
    # reaper 扫描
    # ------------------------------------------------------------------

    def test_sweep_reclaims_expired_sandbox(self):
        sandbox = self.create(ttl_seconds=1)

        later = datetime.now(timezone.utc) + timedelta(seconds=5)
        result = reaper.sweep_once(now=later)

        self.assertEqual(result["reclaimed"], 1)

        row = SandboxService().get(sandbox["sandbox_id"])
        self.assertEqual(row["state"], "reclaimed")
        self.assertEqual(row["reclaim_reason"], "ttl_expired")

        gone = self.client.get(f"/v1/sandboxes/{sandbox['sandbox_id']}")
        self.assertEqual(gone.status_code, 410, gone.text)
        self.assertEqual(gone.json()["error"]["code"], "SANDBOX_GONE")

    def test_sweep_is_idempotent(self):
        self.create(ttl_seconds=1)
        later = datetime.now(timezone.utc) + timedelta(seconds=5)

        first = reaper.sweep_once(now=later)
        second = reaper.sweep_once(now=later)

        self.assertEqual(first["reclaimed"], 1)
        self.assertEqual(second["reclaimed"], 0)

    def test_active_exec_is_cancelled_on_expiry(self):
        sandbox = self.create(ttl_seconds=1)

        accepted = self.client.post(
            f"/v1/sandboxes/{sandbox['sandbox_id']}/exec",
            json={
                "command": [
                    PYTHON, "-c", "import time; time.sleep(30)"
                ],
                "timeout_ms": 60000,
            },
        )
        self.assertEqual(accepted.status_code, 202, accepted.text)
        exec_id = accepted.json()["exec_id"]

        # 等 runner 真的起来，确保 reaper 取消的是活动进程
        for _ in range(200):
            state = self.client.get(f"/v1/exec/{exec_id}").json()["state"]
            if state == "running":
                break
            time.sleep(0.02)

        later = datetime.now(timezone.utc) + timedelta(seconds=5)
        result = reaper.sweep_once(now=later)

        self.assertEqual(result["reclaimed"], 1)
        self.assertEqual(result["cancelled_execs"], 1)

        for _ in range(300):
            body = self.client.get(f"/v1/exec/{exec_id}").json()
            if body["state"] in {"cancelled", "failed"}:
                break
            time.sleep(0.02)

        self.assertEqual(body["state"], "cancelled", body)
        self.assertEqual(body["result_code"], "EXEC_CANCELLED")

    # ------------------------------------------------------------------
    # 惰性过期
    # ------------------------------------------------------------------

    def test_access_after_ttl_returns_410_without_sweep(self):
        sandbox = self.create(key="lazy-key", ttl_seconds=1)
        time.sleep(1.2)

        response = self.client.get(f"/v1/sandboxes/{sandbox['sandbox_id']}")

        self.assertEqual(response.status_code, 410, response.text)

        row = SandboxService().get(sandbox["sandbox_id"])
        self.assertEqual(row["state"], "reclaimed")
        self.assertEqual(row["reclaim_reason"], "ttl_expired")

    # ------------------------------------------------------------------
    # tombstone 与幂等记录保留期
    # ------------------------------------------------------------------

    def test_tombstone_survives_retention_then_is_cleared(self):
        sandbox = self.create(key="retain-key")
        sandbox_id = sandbox["sandbox_id"]

        self.client.post(f"/v1/sandboxes/{sandbox_id}/destroy")

        # 保留期内：还能查到 tombstone（410），同 key create 仍返回同一个沙箱
        same = self.create(key="retain-key")
        self.assertEqual(same["sandbox_id"], sandbox_id)
        self.assertEqual(same["state"], "reclaimed")

        self.assertEqual(reaper.sweep_once()["tombstones_cleared"], 0)

        # 超过 24 小时后：tombstone 与幂等记录一起清掉
        far_future = datetime.now(timezone.utc) + timedelta(hours=25)
        cleared = reaper.sweep_once(now=far_future)
        self.assertEqual(cleared["tombstones_cleared"], 1)

        missing = self.client.get(f"/v1/sandboxes/{sandbox_id}")
        self.assertEqual(missing.status_code, 404, missing.text)
        self.assertEqual(
            missing.json()["error"]["code"], "SANDBOX_NOT_FOUND"
        )

        # 旧 key 现在按新请求处理，拿到的是新沙箱
        fresh = self.create(key="retain-key")
        self.assertNotEqual(fresh["sandbox_id"], sandbox_id)
        self.assertEqual(fresh["state"], "created")

    def test_reclaimed_sandbox_keeps_idempotency_until_tombstone_clears(self):
        sandbox = self.create(key="keep-key")
        self.client.post(f"/v1/sandboxes/{sandbox['sandbox_id']}/destroy")

        with closing(
            __import__("sqlite3").connect(Path(self.temp.name) / "platform.db")
        ) as conn:
            keys = conn.execute(
                "SELECT COUNT(*) FROM sandbox_idempotency WHERE key = ?",
                ("keep-key",),
            ).fetchone()[0]

        self.assertEqual(keys, 1)


if __name__ == "__main__":
    unittest.main()
