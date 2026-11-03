"""D7 流式模型、工具执行和取消留痕（SDK 与网络全部模拟）。

D11 起 allow 工具不再在宿主进程里执行；这里用平台层替身验证「助手 → 平台
exec」的取消传播与 trajectory 顺序。真实进程树的清理证据在
``tests/integration/test_tier1_runner.py`` 与
``tests/integration/test_platform_exec_cancel.py``。
"""

import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from src import agent
from src.agent_stream import run_chat_stream
from src.core.reminder_identity import bind_authenticated_reminder
from src.core.run_control import CancellationToken, ChatCancelled
from src.core.run_store import RunStore
from src.platform.exec_service import ExecOutcome
from tests.helper import isolate_paths


def chunk(*, text=None, finish=None, tool_calls=None, usage=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            delta=SimpleNamespace(content=text, tool_calls=tool_calls or []),
            finish_reason=finish,
        )],
        usage=usage,
    )


def tool_delta(index=0, call_id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index, id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class AgentStreamTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)
        self.request_id = "stream_test_request"
        self.events = []
        self.token = CancellationToken()
        self.identity = bind_authenticated_reminder(
            user_id="alice", request_id=self.request_id, role="owner"
        )
        self.identity.__enter__()
        self.addCleanup(self.identity.__exit__, None, None, None)
        self.db_path = RunStore().db_path

    def stored_run(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            run_id = conn.execute(
                "SELECT id FROM agent_runs WHERE request_id = ?",
                (self.request_id,),
            ).fetchone()[0]
        return RunStore(self.db_path).get_run(run_id, owner_id="alice")

    def tool_stream(self):
        return iter([
            chunk(tool_calls=[tool_delta(call_id="call-1", name="create_reminder")]),
            chunk(tool_calls=[tool_delta(arguments='{"message":"喝水",')]),
            chunk(tool_calls=[tool_delta(arguments='"minutes":5}')], finish="tool_calls"),
            SimpleNamespace(choices=[], usage=SimpleNamespace(
                prompt_tokens=5, completion_tokens=2, total_tokens=7
            )),
        ])

    def test_question_tool_and_incremental_answer_have_real_exec(self):
        final_stream = iter([
            chunk(text="提醒"), chunk(text="已创建", finish="stop")
        ])
        created = {
            "id": 9, "remind_at": "2026-11-02T14:00:00+08:00",
            "delivery": "in_app", "newly_created": True,
        }
        with (
            patch.object(agent, "init_db"),
            patch.object(agent, "_knowledge_context", return_value=None),
            patch.object(agent, "create_reminder", return_value=created),
            patch.object(
                agent.client.chat.completions, "create",
                side_effect=[self.tool_stream(), final_stream],
            ) as model,
        ):
            reply = run_chat_stream(
                "五分钟后提醒我喝水", emit=self.events.append,
                cancel_token=self.token,
            )

        self.assertEqual(reply, "提醒已创建")
        self.assertEqual(model.call_count, 2)
        self.assertTrue(all(call.kwargs["stream"] for call in model.call_args_list))
        deltas = [event["text"] for event in self.events if event["type"] == "text_delta"]
        self.assertEqual(deltas, ["提醒", "已创建"])
        self.assertEqual(self.events[-1]["type"], "done")

        run = self.stored_run()
        self.assertEqual(run["status"], "succeeded")
        steps = run["turns"][0]["steps"]
        self.assertEqual([step["kind"] for step in steps], ["user", "assistant", "tool", "assistant"])
        execution = steps[2]["exec"]
        self.assertEqual(execution["tool_name"], "create_reminder")
        self.assertEqual(execution["status"], "succeeded")
        self.assertEqual(execution["request"], {"message": "喝水", "minutes": 5})

    def test_cancel_enters_tool_and_marks_run_step_exec_cancelled(self):
        entered = Event()
        interrupted = Event()

        def fake_platform(
            tool_call,
            *,
            cancel_token=None,
            sandbox_id=None,
            on_started=None,
            on_cancel_requested=None,
            require_allow=True,
        ):
            on_started("sbx_stream_test")
            unregister = cancel_token.register_canceller(interrupted.set)
            try:
                entered.set()
                self.assertTrue(interrupted.wait(2))
            finally:
                unregister()
            on_cancel_requested("2026-11-03T00:00:00+00:00")
            return ExecOutcome(
                sandbox_id="sbx_stream_test",
                status="cancelled",
                exit_code=None,
                stdout="",
                stderr="",
                truncated=False,
                error_code="EXEC_CANCELLED",
                duration_ms=1,
                cancel_requested_at="2026-11-03T00:00:00+00:00",
            )

        with (
            patch.object(agent, "init_db"),
            patch.object(agent, "_knowledge_context", return_value=None),
            patch.object(
                agent, "execute_tool_on_platform", side_effect=fake_platform
            ),
            patch.object(agent.client.chat.completions, "create",
                         return_value=self.tool_stream()),
        ):
            from contextvars import copy_context
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    copy_context().run, run_chat_stream, "五分钟后提醒我喝水",
                    emit=self.events.append, cancel_token=self.token,
                )
                self.assertTrue(entered.wait(5))
                self.token.cancel()
                with self.assertRaises(ChatCancelled):
                    future.result(timeout=5)

        self.assertEqual(self.events[-1]["type"], "cancelled")
        run = self.stored_run()
        self.assertEqual(run["status"], "cancelled")
        tool_step = next(
            step for step in run["turns"][0]["steps"] if step["kind"] == "tool"
        )
        self.assertEqual(tool_step["status"], "cancelled")
        self.assertEqual(tool_step["exec"]["status"], "cancelled")
        self.assertEqual(tool_step["exec"]["sandbox_id"], "sbx_stream_test")

        events = [
            event for event in RunStore(self.db_path).list_events(run["id"])
            if event["exec_id"] is not None
        ]
        self.assertEqual(
            [(event["event_type"], event["status"]) for event in events],
            [
                ("exec_started", "running"),
                ("exec_cancel_requested", "cancel_requested"),
                ("exec_finished", "cancelled"),
            ],
        )
        self.assertEqual(
            {event["exec_id"] for event in events}, {tool_step["exec_id"]}
        )


if __name__ == "__main__":
    unittest.main()
