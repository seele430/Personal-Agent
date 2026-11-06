"""route_decided 事件写入 trajectory。"""

from __future__ import annotations

import unittest

from pathlib import Path
from tempfile import TemporaryDirectory

from src.core.run_store import RunStore
from src.routing import Router
from src.routing.events import EVENT_TYPE, record_route_decision


class RouteEventTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)

        self.store = RunStore(Path(self.temp.name) / "runs.db")

        self.run = self.store.create_run(owner_id="tester")

        # 真实链路里 run_started 由 Agent 先写；这里补上，验证顺序契约。
        self.store.append_event(
            self.run["id"],
            event_type="run_started",
            status="running",
        )

        self.router = Router()

    def record(self, message: str):
        decision = self.router.decide(message)
        return record_route_decision(self.store, self.run["id"], decision)

    def test_route_event_is_written_after_run_started(self):
        self.record("分析这个 CSV 的缺失值，并生成一张图")

        events = self.store.list_events(self.run["id"])

        self.assertEqual(events[0]["event_type"], "run_started")
        self.assertEqual(events[1]["event_type"], EVENT_TYPE)
        self.assertEqual(events[1]["status"], "routed")

    def test_event_payload_carries_decision(self):
        self.record("分析这个 CSV 的缺失值，并生成一张图")

        payload = self.store.list_events(self.run["id"])[1]["payload"]

        self.assertEqual(payload["intent"], "data.analysis")
        self.assertEqual(payload["capability"], "sandbox_exec")
        self.assertEqual(payload["execution_type"], "local_sandbox")
        self.assertFalse(payload["requires_approval"])
        self.assertIn("candidates", payload)

    def test_high_risk_event_marks_approval(self):
        self.record("删掉 data 目录")

        payload = self.store.list_events(self.run["id"])[1]["payload"]

        self.assertEqual(payload["risk"], "high")
        self.assertTrue(payload["requires_approval"])

    def test_clarify_event(self):
        self.record("分析并运行这个脚本")

        row = self.store.list_events(self.run["id"])[1]

        self.assertEqual(row["status"], "clarify")
        self.assertEqual(row["payload"]["decision"], "clarify")
        self.assertIsNone(row["payload"]["intent"])

    def test_event_has_no_node_or_sandbox(self):
        self.record("分析这个 CSV")

        row = self.store.list_events(self.run["id"])[1]["payload"]

        self.assertNotIn("node_id", row)
        self.assertNotIn("sandbox_id", row)
        self.assertNotIn("model_policy", row)

    def test_legacy_event_with_model_policy_still_readable(self):
        # 历史轨迹里可能还有 model_policy；删除字段不能影响旧事件读取。
        self.store.append_event(
            self.run["id"],
            event_type="route_decided",
            status="routed",
            payload={
                "decision": "route",
                "intent": "data.analysis",
                "model_policy": "strong",
            },
        )

        events = self.store.list_events(self.run["id"])

        self.assertEqual(events[-1]["event_type"], "route_decided")
        self.assertEqual(events[-1]["payload"]["model_policy"], "strong")


if __name__ == "__main__":
    unittest.main()
