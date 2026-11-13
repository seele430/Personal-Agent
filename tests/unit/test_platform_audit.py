"""编排 coding 的平台审计事件：白名单登记 + 记录往返。"""

from __future__ import annotations

import unittest

from tempfile import TemporaryDirectory

from src.platform.audit import EVENTS, AuditLog
from tests.helper import isolate_paths


class OrchestratorAuditEventTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)

    def test_events_whitelisted(self):
        self.assertIn("orchestrator.coding_started", EVENTS)
        self.assertIn("orchestrator.coding_finished", EVENTS)

    def test_record_roundtrip(self):
        log = AuditLog()
        row = log.record(
            "orchestrator.coding_finished",
            outcome="failed",
            code="code_timeout",
            actor="orchestrator",
            sandbox_id="sbx_" + "0" * 32,
            exec_id="exec_x",
            detail={"run_id": "run-x", "step": "coding"},
        )

        self.assertEqual(row["event"], "orchestrator.coding_finished")
        self.assertEqual(row["code"], "code_timeout")
        self.assertEqual(row["exec_id"], "exec_x")

    def test_unknown_event_still_rejected(self):
        with self.assertRaises(ValueError):
            AuditLog().record("orchestrator.coding_hacked", outcome="ok")


if __name__ == "__main__":
    unittest.main()
