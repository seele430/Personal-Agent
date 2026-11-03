"""D11 全链路取消：助手 → 平台 exec → runner 进程树 → trajectory。

这里用 ``run_chat_stream`` 驱动真实链路（模型调用是本地替身），进程是真的：
探针在 runner 子进程里派生「父 + 子」，用例确认两个 PID 都被清掉，并检查
trajectory 里取消请求与最终 cancelled 的顺序。

真实 HTTP / SSE 断流的验收在 ``scripts/verify_d11_cancel.py``。
"""

from __future__ import annotations

import sqlite3
import sys
import time
import unittest

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from contextvars import copy_context
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from src import agent
from src.agent_stream import run_chat_stream
from src.core.reminder_identity import bind_authenticated_reminder
from src.core.run_control import CancellationToken, ChatCancelled
from src.core.run_store import RunStore
from src.platform.exec_service import PlatformExecutionError
from src.platform.process_tree import process_is_alive
from tests.helper import isolate_paths
from tests.integration.tier1_probe import (
    wait_for_pids,
    register_probe_tool,
    unregister_probe_tool,
)


def chunk(*, text=None, finish=None, tool_calls=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=text, tool_calls=tool_calls or []),
                finish_reason=finish,
            )
        ]
    )


def tool_delta(index=0, call_id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def tool_stream(name, arguments: str):
    """把一次工具调用拆成 SDK 流式片段。"""

    return iter(
        [
            chunk(tool_calls=[tool_delta(call_id="call-1", name=name)]),
            chunk(tool_calls=[tool_delta(arguments=arguments[: len(arguments) // 2])]),
            chunk(tool_calls=[tool_delta(arguments=arguments[len(arguments) // 2 :])], finish="tool_calls"),
        ]
    )


class PlatformExecCancelTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)

        self.pid_file = Path(self.temp.name) / "pids.json"
        self.request_id = "d11_exec_cancel"
        self.events: list[dict] = []
        self.token = CancellationToken()

        identity = bind_authenticated_reminder(
            user_id="alice", request_id=self.request_id, role="member"
        )
        identity.__enter__()
        self.addCleanup(identity.__exit__, None, None, None)

        self.db_path = RunStore().db_path
        self.addCleanup(unregister_probe_tool)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def run_row(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT * FROM agent_runs WHERE request_id = ?",
                (self.request_id,),
            ).fetchone()

    def exec_rows(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT * FROM agent_execs ORDER BY created_at"
            ).fetchall()

    def stored_run(self) -> dict:
        run = self.run_row()
        return RunStore(self.db_path).get_run(run["id"], owner_id="alice")

    def events_of(self, run_id: str) -> list[dict]:
        return RunStore(self.db_path).list_events(run_id)

    def run_in_thread(self):
        return ThreadPoolExecutor(max_workers=1)

    # ------------------------------------------------------------------
    # 1. 取消：进程树清空之后才允许落 cancelled
    # ------------------------------------------------------------------

    def test_cancel_propagates_to_runner_and_records_events(self):
        register_probe_tool(sys.executable, self.pid_file)

        with (
            patch.object(agent, "init_db"),
            patch.object(agent, "_knowledge_context", return_value=None),
            patch.object(
                agent.client.chat.completions,
                "create",
                return_value=tool_stream("tier1_probe_tree", "{}"),
            ),
        ):
            with self.run_in_thread() as pool:
                future = pool.submit(
                    copy_context().run,
                    run_chat_stream,
                    "启动探针",
                    emit=self.events.append,
                    cancel_token=self.token,
                )

                # 探针把父子 PID 写进文件之后，进程树才真的起来，
                # 这时取消才有东西可清。
                pids = wait_for_pids(self.pid_file)
                self.token.cancel()

                with self.assertRaises(ChatCancelled):
                    future.result(timeout=60)

        # 真实进程已经消失
        self.assertFalse(process_is_alive(pids["parent"]))
        self.assertFalse(process_is_alive(pids["child"]))

        run = self.stored_run()
        self.assertEqual(run["status"], "cancelled")

        tool_step = next(
            step for step in run["turns"][0]["steps"] if step["kind"] == "tool"
        )
        self.assertEqual(tool_step["status"], "cancelled")
        self.assertEqual(tool_step["exec"]["status"], "cancelled")
        self.assertEqual(tool_step["exec"]["error_code"], "EXEC_CANCELLED")

        events = [
            event for event in self.events_of(run["id"])
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

        for event in events:
            self.assertEqual(event["schema_version"], 1)
            self.assertEqual(event["run_id"], run["id"])
            self.assertEqual(event["exec_id"], tool_step["exec_id"])
            self.assertEqual(event["step_id"], tool_step["id"])
            self.assertEqual(
                event["sandbox_id"], tool_step["exec"]["sandbox_id"]
            )

        # 取消请求必须早于最终 cancelled
        self.assertLess(events[1]["sequence"], events[2]["sequence"])

    # ------------------------------------------------------------------
    # 2. ask：进入审批门，批准之前没有 exec（D13）
    # ------------------------------------------------------------------

    def test_ask_tool_waits_for_approval_and_never_creates_exec(self):
        final_stream = iter([chunk(text="好的", finish="stop")])

        with (
            patch.object(agent, "init_db"),
            patch.object(agent, "_knowledge_context", return_value=None),
            patch.object(
                agent.client.chat.completions,
                "create",
                side_effect=[
                    tool_stream(
                        "update_reminder", '{"reminder_id": 1, "message": "改一下"}'
                    ),
                    final_stream,
                ],
            ),
        ):
            with self.run_in_thread() as pool:
                future = pool.submit(
                    copy_context().run,
                    run_chat_stream,
                    "改一下提醒",
                    emit=self.events.append,
                    cancel_token=self.token,
                )

                approval = self.wait_pending_approval()

                # 审批还没决定时不能有任何 exec
                self.assertEqual(self.exec_rows(), [])

                self.store().decide_approval(
                    approval["id"], decision="reject", decided_by="alice"
                )
                future.result(timeout=60)

        run = self.stored_run()
        tool_step = next(
            step for step in run["turns"][0]["steps"] if step["kind"] == "tool"
        )
        self.assertEqual(tool_step["status"], "denied")
        self.assertIsNone(tool_step["exec"])
        self.assertEqual(self.exec_rows(), [])

        self.assertEqual(
            [
                (event["event_type"], event["status"])
                for event in self.events_of(run["id"])
                if event["event_type"].startswith("tool_approval")
            ],
            [
                ("tool_approval_requested", "pending"),
                ("tool_approval_decided", "rejected"),
            ],
        )

    def store(self) -> RunStore:
        return RunStore(self.db_path)

    def wait_pending_approval(self, timeout: float = 15.0) -> dict:
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            row = self.run_row()

            if row is not None:
                pending = self.store().list_approvals(row["id"], status="pending")

                if pending:
                    return pending[0]

            time.sleep(0.02)

        raise AssertionError("等不到 pending 审批")

    # ------------------------------------------------------------------
    # 3. 平台超时：exec 落 timed_out，进程树同样要清
    # ------------------------------------------------------------------

    def test_platform_timeout_marks_exec_timed_out(self):
        register_probe_tool(sys.executable, self.pid_file, timeout_ms=1500)

        with (
            patch.object(agent, "init_db"),
            patch.object(agent, "_knowledge_context", return_value=None),
            patch.object(
                agent.client.chat.completions,
                "create",
                return_value=tool_stream("tier1_probe_tree", "{}"),
            ),
        ):
            with self.assertRaises(PlatformExecutionError):
                run_chat_stream(
                    "跑一个超时的探针",
                    emit=self.events.append,
                    cancel_token=self.token,
                )

        rows = self.exec_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "timed_out")
        self.assertEqual(rows[0]["error_code"], "EXEC_TIMED_OUT")

        pids = wait_for_pids(self.pid_file)
        self.assertFalse(process_is_alive(pids["parent"]))
        self.assertFalse(process_is_alive(pids["child"]))

        run = self.stored_run()
        self.assertEqual(run["status"], "failed")

        events = [
            event for event in self.events_of(run["id"])
            if event["exec_id"] is not None
        ]
        self.assertEqual(
            [(event["event_type"], event["status"]) for event in events],
            [("exec_started", "running"), ("exec_finished", "timed_out")],
        )

        # 平台失败要能定位到具体执行
        self.assertEqual(events[-1]["exec_id"], rows[0]["id"])
        self.assertEqual(events[-1]["payload"]["error_code"], "EXEC_TIMED_OUT")

    def tearDown(self):
        # 用例失败时也不留进程
        if not self.pid_file.is_file():
            return

        try:
            from tests.integration.tier1_probe import read_pids

            pids = read_pids(self.pid_file)
        except Exception:  # noqa: BLE001
            return

        leftovers = [pid for pid in pids.values() if process_is_alive(pid)]

        if leftovers:
            for pid in leftovers:
                subprocess_kill(pid)

            raise AssertionError(f"遗留进程：{leftovers}")


def subprocess_kill(pid: int) -> None:
    """仅用于测试兜底：按 PID 结束探针进程。"""

    import subprocess

    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(pid)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


if __name__ == "__main__":
    unittest.main()
