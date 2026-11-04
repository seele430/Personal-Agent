"""D23 G2 全链路：一次响应触发 3 个工具，全部走平台沙箱并落轨迹。"""

from __future__ import annotations

import sqlite3
import sys
import unittest

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from src import agent
from src.agent_stream import run_chat_stream
from src.core.reminder_identity import bind_authenticated_reminder
from src.core.run_control import CancellationToken
from src.core.run_store import RunStore
from tests.helper import isolate_paths

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "g2_echo",
        "description": "G2 演示用命令工具",
        "parameters": {"type": "object", "properties": {}},
    },
}


def chunk(*, text=None, finish=None, tool_calls=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=text, tool_calls=tool_calls or []),
                finish_reason=finish,
            )
        ]
    )


def call_delta(index, call_id, name, arguments="{}"):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class MultiToolChainTest(unittest.TestCase):
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

        agent.TOOL_REGISTRY.register(
            TOOL_SCHEMA,
            policy="allow",
            handler=lambda *args, **kwargs: {"success": True},
            execution={
                "kind": "command",
                "argv": [sys.executable, "-c", "print('g2')"],
            },
        )
        self.addCleanup(agent.TOOL_REGISTRY.unregister, "g2_echo")

        self.request_id = "d23_chain"
        self.token = CancellationToken()
        self.events: list[dict] = []
        self.db_path = RunStore().db_path

        identity = bind_authenticated_reminder(
            user_id="alice", request_id=self.request_id, role="member"
        )
        identity.__enter__()
        self.addCleanup(identity.__exit__, None, None, None)

    # ------------------------------------------------------------------

    def tool_stream(self, calls):
        return iter(
            [
                chunk(
                    tool_calls=[
                        call_delta(index, call_id, name, arguments)
                        for index, (call_id, name, arguments) in enumerate(calls)
                    ],
                    finish="tool_calls",
                )
            ]
        )

    def run_with(self, calls):
        final = iter([chunk(text="三个工具都跑完了", finish="stop")])

        with (
            patch.object(agent, "init_db"),
            patch.object(agent, "_knowledge_context", return_value=None),
            patch.object(
                agent.client.chat.completions,
                "create",
                side_effect=[self.tool_stream(calls), final],
            ),
        ):
            return run_chat_stream(
                "把这些都跑一遍",
                emit=self.events.append,
                cancel_token=self.token,
            )

    def exec_rows(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(
                "SELECT tool_name, status FROM agent_execs ORDER BY created_at"
            ).fetchall()

    def run_id(self) -> str:
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(
                "SELECT id FROM agent_runs WHERE request_id = ?",
                (self.request_id,),
            ).fetchone()[0]

    # ------------------------------------------------------------------
    # 默认行为不变
    # ------------------------------------------------------------------

    def test_default_limit_still_rejects_two_tools(self):
        with self.assertRaises(RuntimeError):
            self.run_with(
                [
                    ("c1", "list_dir", "{}"),
                    ("c2", "g2_echo", "{}"),
                ]
            )

        self.assertEqual(self.exec_rows(), [])

    # ------------------------------------------------------------------
    # G2：一句话 3 个工具
    # ------------------------------------------------------------------

    def test_three_tools_run_in_sandbox_and_land_in_trajectory(self):
        with patch.object(agent, "MAX_TOOL_CALLS_PER_TURN", 3):
            reply = self.run_with(
                [
                    ("c1", "list_dir", "{}"),
                    ("c2", "g2_echo", "{}"),
                    ("c3", "list_dir", '{"path": "."}'),
                ]
            )

        self.assertEqual(reply, "三个工具都跑完了")

        rows = self.exec_rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual({row[0] for row in rows}, {"list_dir", "g2_echo"})
        self.assertTrue(all(row[1] == "succeeded" for row in rows))

        run = RunStore(self.db_path).get_run(self.run_id(), owner_id="alice")
        tool_steps = [
            step for step in run["turns"][0]["steps"] if step["kind"] == "tool"
        ]
        self.assertEqual(len(tool_steps), 3)

        events = RunStore(self.db_path).list_events(self.run_id())
        exec_events = [
            event for event in events if event["exec_id"] is not None
        ]
        self.assertEqual(
            [event["event_type"] for event in exec_events],
            [
                "exec_started", "exec_finished",
                "exec_started", "exec_finished",
                "exec_started", "exec_finished",
            ],
        )
        # 三个 exec 都关联到同一次 run 的不同 step
        self.assertEqual(
            len({event["step_id"] for event in exec_events}), 3
        )

    def test_reminder_cap_is_independent_of_turn_limit(self):
        with patch.object(agent, "MAX_TOOL_CALLS_PER_TURN", 3):
            with self.assertRaises(RuntimeError):
                self.run_with(
                    [
                        ("c1", "create_reminder", '{"message": "喝水", "minutes": 5}'),
                        ("c2", "create_reminder", '{"message": "休息", "minutes": 9}'),
                    ]
                )

        self.assertEqual(self.exec_rows(), [])


if __name__ == "__main__":
    unittest.main()
