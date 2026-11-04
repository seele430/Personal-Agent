"""D30 压测暴露的回归：默认节点必须自己续租，否则 30 秒后建不了沙箱。"""

from __future__ import annotations

import unittest

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.main import app
from src.platform.node_registry import DEFAULT_NODE_ID, NodeRegistry
from tests.helper import isolate_paths

BASE = datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)


class DefaultNodeRenewalTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
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

    def test_ensure_default_node_renews_expired_lease(self):
        registry = NodeRegistry()
        node = registry.ensure_default_node(now=BASE)
        self.assertEqual(node["generation"], 1)

        # 30 秒没动静 → 租约过期
        expired = registry.get(DEFAULT_NODE_ID, now=BASE + timedelta(seconds=60))
        self.assertEqual(expired["state"], "not_ready")
        self.assertFalse(expired["accepting"])

        # 平台还活着 → ensure_default_node 续租（代际不变）
        renewed = registry.ensure_default_node(now=BASE + timedelta(seconds=61))
        self.assertEqual(renewed["generation"], 1)
        self.assertEqual(renewed["state"], "ready")
        self.assertTrue(renewed["accepting"])

    def test_removed_default_node_is_not_revived(self):
        registry = NodeRegistry()
        registry.ensure_default_node(now=BASE)
        registry.drain(DEFAULT_NODE_ID, now=BASE)
        registry.remove(DEFAULT_NODE_ID, now=BASE)

        after = registry.ensure_default_node(now=BASE + timedelta(seconds=5))

        self.assertTrue(after["removed"])
        self.assertFalse(after["accepting"])

    def test_create_sandbox_after_lease_would_have_expired(self):
        # 第一次 create 注册节点
        client = TestClient(app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)

        created = client.post(
            "/v1/sandboxes",
            json={
                "tier": "tier1",
                "workspace_id": "ws_renew",
                "ttl_seconds": 600,
            },
            headers={"Idempotency-Key": "renew-1"},
        )
        self.assertEqual(created.status_code, 201, created.text)

        # 把 last_heartbeat 拨回 5 分钟前，模拟「闲置很久」
        import sqlite3

        from src import config

        with sqlite3.connect(str(config.paths.platform_db)) as conn:
            conn.execute(
                "UPDATE agent_nodes SET last_heartbeat = ? WHERE node_id = ?",
                (
                    (BASE - timedelta(minutes=5)).isoformat(),
                    DEFAULT_NODE_ID,
                ),
            )
            conn.commit()

        again = client.post(
            "/v1/sandboxes",
            json={
                "tier": "tier1",
                "workspace_id": "ws_renew",
                "ttl_seconds": 600,
            },
            headers={"Idempotency-Key": "renew-2"},
        )

        self.assertEqual(again.status_code, 201, again.text)
        self.assertEqual(
            NodeRegistry().get(DEFAULT_NODE_ID)["state"], "ready"
        )


if __name__ == "__main__":
    unittest.main()
