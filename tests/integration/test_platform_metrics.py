"""D25' 指标 + D26' JSONL 日志的验收。"""

from __future__ import annotations

import io
import json
import logging
import sys
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.core.log_json import JsonLineFormatter
from src.core.trace_context import bind_trace
from src.main import app
from tests.helper import isolate_paths, wait_for_exec_worker

METRIC_NAMES = [
    "personal_agent_execs_total",
    "personal_agent_exec_duration_seconds_count",
    "personal_agent_exec_duration_seconds_sum",
    "personal_agent_exec_active",
    "personal_agent_queue_depth",
    "personal_agent_sandboxes",
    "personal_agent_nodes_ready",
]


class MetricsTest(unittest.TestCase):
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

        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def scrape(self) -> str:
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/plain", response.headers["content-type"])
        return response.text

    def test_all_core_metrics_are_exposed(self):
        body = self.scrape()

        for name in METRIC_NAMES:
            self.assertIn(name, body)

        self.assertIn("# TYPE personal_agent_execs_total counter", body)
        self.assertIn("# TYPE personal_agent_sandboxes gauge", body)

    def test_metrics_reflect_real_activity(self):
        before = self.scrape()

        created = self.client.post(
            "/v1/sandboxes",
            json={
                "tier": "tier1",
                "workspace_id": "ws_metrics",
                "ttl_seconds": 600,
            },
            headers={"Idempotency-Key": "metrics-key"},
        )
        sandbox_id = created.json()["sandbox_id"]

        accepted = self.client.post(
            f"/v1/sandboxes/{sandbox_id}/exec",
            json={"command": [sys.executable, "-c", "print('ok')"]},
        )
        exec_id = accepted.json()["exec_id"]

        for _ in range(500):
            body = self.client.get(f"/v1/exec/{exec_id}").json()
            if body["state"] in {"succeeded", "failed", "cancelled", "timed_out"}:
                break
            import time

            time.sleep(0.02)

        wait_for_exec_worker(exec_id)
        after = self.scrape()

        self.assertIn('personal_agent_execs_total{status="succeeded"} 1', after)
        # exec 跑完后沙箱从 created 迁到 idle（D20 状态机），两种都算数
        self.assertRegex(
            after, r'personal_agent_sandboxes\{state="(created|idle)"\} 1'
        )
        self.assertIn("personal_agent_exec_duration_seconds_count 1", after)
        self.assertNotEqual(before, after)

    def test_queue_depth_matches_limits(self):
        body = self.scrape()

        self.assertIn('personal_agent_queue_depth{state="active"} 0', body)
        self.assertIn('personal_agent_queue_depth{state="queued"} 0', body)


class JsonLogTest(unittest.TestCase):
    def test_formatter_emits_json_with_trace_id(self):
        formatter = JsonLineFormatter()
        record = logging.LogRecord(
            name="personal_agent.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="执行完成",
            args=(),
            exc_info=None,
        )

        with bind_trace("a" * 32):
            line = formatter.format(record)

        payload = json.loads(line)

        self.assertEqual(payload["msg"], "执行完成")
        self.assertEqual(payload["level"], "INFO")
        self.assertEqual(payload["trace_id"], "a" * 32)
        self.assertIn("ts", payload)

    def test_formatter_omits_trace_when_unbound(self):
        formatter = JsonLineFormatter()
        record = logging.LogRecord(
            name="t", level=logging.WARNING, pathname=__file__,
            lineno=1, msg="无 trace", args=(), exc_info=None,
        )

        payload = json.loads(formatter.format(record))

        self.assertNotIn("trace_id", payload)

    def test_json_logging_switch(self):
        from src.core import log_json

        with patch.dict("os.environ", {"PERSONAL_AGENT_LOG_JSON": "1"}):
            self.assertTrue(log_json.json_logging_enabled())

        with patch.dict("os.environ", {"PERSONAL_AGENT_LOG_JSON": "0"}):
            self.assertFalse(log_json.json_logging_enabled())


if __name__ == "__main__":
    unittest.main()
