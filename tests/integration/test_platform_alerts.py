"""D37 告警闭环：阈值 → 自动驱逐 → 审计留痕。"""

from __future__ import annotations

import unittest

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.platform import alerts
from src.platform.audit import AuditLog
from src.platform.node_registry import DEFAULT_NODE_ID, NodeRegistry
from tests.helper import isolate_paths

BASE = datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)


class AlertsTest(unittest.TestCase):
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

    def test_healthy_node_has_no_alerts(self):
        NodeRegistry().ensure_default_node(now=BASE)

        self.assertEqual(alerts.evaluate(now=BASE + timedelta(seconds=5)), [])

    def test_expired_node_fires_and_gets_cordoned(self):
        NodeRegistry().ensure_default_node(now=BASE)

        found = alerts.evaluate(now=BASE + timedelta(seconds=120))

        self.assertEqual(len(found), 1, found)
        self.assertEqual(found[0].code, "NODE_NOT_READY")
        self.assertEqual(found[0].node_id, DEFAULT_NODE_ID)

        applied = alerts.apply(found)

        self.assertEqual(applied[0]["actions"], ["cordon", "drain"])

        node = NodeRegistry().get(DEFAULT_NODE_ID)
        self.assertTrue(node["cordoned"])
        self.assertTrue(node["draining"])

        records = [
            item
            for item in AuditLog().list(limit=10)
            if item["event"] == "alert.fired"
        ]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["code"], "NODE_NOT_READY")
        self.assertEqual(records[0]["detail"]["actions"], ["cordon", "drain"])

    def test_queue_backlog_threshold(self):
        with patch.object(
            alerts,
            "get_queue",
            return_value=type(
                "Q", (), {"snapshot": lambda self: {
                    "active": 4, "queued": 9,
                    "max_active": 4, "max_queued": 16,
                }}
            )(),
        ):
            found = alerts.evaluate()

        self.assertEqual([item.code for item in found], ["QUEUE_BACKLOG"])


if __name__ == "__main__":
    unittest.main()
