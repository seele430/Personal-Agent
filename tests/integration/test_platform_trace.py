"""D24 traceparent：宿主注入、沙箱可见、落库可查，一条 run 共用 trace-id。"""

from __future__ import annotations

import sqlite3
import sys
import unittest

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import agent
from src.agent_stream import run_chat_stream
from src.core.reminder_identity import bind_authenticated_reminder
from src.core.run_control import CancellationToken
from src.core.run_store import RunStore
from src.main import app
from src.platform.trace_context import TRACEPARENT_ENV, is_valid, parse
from tests.helper import isolate_paths, wait_for_exec_worker

PRINT_TP = [
    sys.executable,
    "-c",
    "import os; print(os.environ.get('TRACEPARENT'))",
]


class TraceApiTest(unittest.TestCase):
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

    def create(self) -> str:
        response = self.client.post(
            "/v1/sandboxes",
            json={
                "tier": "tier1",
                "workspace_id": "ws_trace",
                "ttl_seconds": 600,
            },
            headers={"Idempotency-Key": "trace-key"},
        )
        return response.json()["sandbox_id"]

    def run_exec(self, sandbox_id: str, headers: dict | None = None) -> dict:
        accepted = self.client.post(
            f"/v1/sandboxes/{sandbox_id}/exec",
            json={"command": PRINT_TP, "timeout_ms": 30000},
            headers=headers or {},
        )
        self.assertEqual(accepted.status_code, 202, accepted.text)
        exec_id = accepted.json()["exec_id"]

        for _ in range(2000):
            body = self.client.get(f"/v1/exec/{exec_id}").json()
            if body["state"] in {"succeeded", "failed", "cancelled", "timed_out"}:
                wait_for_exec_worker(exec_id)
                return body
            import time

            time.sleep(0.01)

        self.fail("exec 没有结束")

    def test_command_sees_generated_traceparent(self):
        sandbox_id = self.create()
        body = self.run_exec(sandbox_id)

        self.assertEqual(body["state"], "succeeded", body)
        self.assertTrue(is_valid(body["traceparent"]), body)
        self.assertEqual(body["stdout"].strip(), body["traceparent"])

    def test_caller_traceparent_is_propagated(self):
        sandbox_id = self.create()
        supplied = (
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        )

        body = self.run_exec(
            sandbox_id, headers={"traceparent": supplied}
        )

        self.assertEqual(body["traceparent"], supplied)
        self.assertEqual(body["stdout"].strip(), supplied)

    def test_invalid_traceparent_is_replaced(self):
        sandbox_id = self.create()
        body = self.run_exec(
            sandbox_id, headers={"traceparent": "garbage"}
        )

        self.assertTrue(is_valid(body["traceparent"]), body)
        self.assertEqual(body["stdout"].strip(), body["traceparent"])

    def test_each_exec_has_its_own_span(self):
        sandbox_id = self.create()
        first = self.run_exec(sandbox_id)
        second = self.run_exec(sandbox_id)

        self.assertNotEqual(first["traceparent"], second["traceparent"])

    def test_traceparent_visible_on_get(self):
        sandbox_id = self.create()
        body = self.run_exec(sandbox_id)

        fetched = self.client.get(f"/v1/exec/{body['exec_id']}").json()

        self.assertEqual(fetched["traceparent"], body["traceparent"])


class TraceRunTest(unittest.TestCase):
    """同一条 run 的多次 exec：trace-id 相同，span-id 各不相同。"""

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

        self.request_id = "d24_trace"
        self.token = CancellationToken()
        self.db_path = RunStore().db_path

        identity = bind_authenticated_reminder(
            user_id="alice", request_id=self.request_id, role="member"
        )
        identity.__enter__()
        self.addCleanup(identity.__exit__, None, None, None)

    def test_run_execs_share_trace_id(self):
        def chunk(*, text=None, finish=None, tool_calls=None):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=text, tool_calls=tool_calls or []
                        ),
                        finish_reason=finish,
                    )
                ]
            )

        def delta(index, call_id, name, arguments="{}"):
            return SimpleNamespace(
                index=index,
                id=call_id,
                function=SimpleNamespace(name=name, arguments=arguments),
            )

        tool_stream = iter(
            [
                chunk(
                    tool_calls=[
                        delta(0, "t1", "list_dir"),
                        delta(1, "t2", "list_dir"),
                    ],
                    finish="tool_calls",
                )
            ]
        )
        final = iter([chunk(text="跑完了", finish="stop")])

        with (
            patch.object(agent, "init_db"),
            patch.object(agent, "_knowledge_context", return_value=None),
            patch.object(agent, "MAX_TOOL_CALLS_PER_TURN", 3),
            patch.object(
                agent.client.chat.completions,
                "create",
                side_effect=[tool_stream, final],
            ),
        ):
            reply = run_chat_stream(
                "并行跑两个", emit=lambda event: None, cancel_token=self.token
            )

        self.assertEqual(reply, "跑完了")

        with closing(sqlite3.connect(self.db_path)) as conn:
            run_id = conn.execute(
                "SELECT id FROM agent_runs WHERE request_id = ?",
                (self.request_id,),
            ).fetchone()[0]

        events = RunStore(self.db_path).list_events(run_id)
        traceparents = [
            event["payload"]["traceparent"]
            for event in events
            if event["event_type"] == "exec_started"
        ]

        self.assertEqual(len(traceparents), 2)
        self.assertTrue(all(is_valid(item) for item in traceparents))

        trace_ids = {parse(item)[0] for item in traceparents}
        span_ids = {parse(item)[1] for item in traceparents}

        self.assertEqual(len(trace_ids), 1)  # 同一条 run 共用 trace-id
        self.assertEqual(len(span_ids), 2)  # 每次 exec 有自己的 span


if __name__ == "__main__":
    unittest.main()
