"""D13 审批门：ask 工具在批准之前不产生 exec。

设计见 ``docs/d13-approval-plan.md``。模型调用是本地替身；平台、进程、
SQLite 和审批状态都是真的。
"""

from __future__ import annotations

import sqlite3
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
from src.auth_api import get_current_user
from src.core import approvals
from src.core.reminder_identity import bind_authenticated_reminder
from src.core.run_control import CancellationToken, ChatCancelled
from src.core.run_store import RunStore
from src.core.tool_registry import ToolDenied
from src.main import app
from tests.helper import isolate_paths

import src.chat_secure_api as chat_api

from fastapi.testclient import TestClient


def chunk(*, text=None, finish=None, tool_calls=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=text, tool_calls=tool_calls or []),
                finish_reason=finish,
            )
        ]
    )


def tool_delta(name=None, arguments=None, call_id=None):
    return SimpleNamespace(
        index=0,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def tool_stream(name, arguments: str):
    half = len(arguments) // 2

    return iter(
        [
            chunk(tool_calls=[tool_delta(name=name, call_id="call-approval")]),
            chunk(tool_calls=[tool_delta(arguments=arguments[:half])]),
            chunk(tool_calls=[tool_delta(arguments=arguments[half:])], finish="tool_calls"),
        ]
    )


class ApprovalGateTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)

        patcher = patch.dict(
            "os.environ",
            {
                "PERSONAL_AGENT_SANDBOX_ROOT": str(Path(self.temp.name) / "sandboxes"),
                "PERSONAL_AGENT_APPROVAL_TIMEOUT_SECONDS": "30",
            },
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.request_id = "d13_approval"
        self.token = CancellationToken()
        self.events: list[dict] = []
        self.db_path = RunStore().db_path

        identity = bind_authenticated_reminder(
            user_id="alice", request_id=self.request_id, role="member"
        )
        identity.__enter__()
        self.addCleanup(identity.__exit__, None, None, None)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def store(self) -> RunStore:
        return RunStore(self.db_path)

    def run_id(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT id FROM agent_runs WHERE request_id = ?",
                (self.request_id,),
            ).fetchone()

        return None if row is None else row[0]

    def exec_rows(self) -> list[tuple]:
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(
                "SELECT id, status, error_code FROM agent_execs"
            ).fetchall()

    def wait_pending(self, timeout: float = 15.0) -> dict:
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if self.future.done():
                # 线程提前结束说明 run 没走到审批门，直接暴露原因。
                raise AssertionError(
                    f"run 在到达审批前结束：{self.future.exception()!r}；"
                    f"events={[event.get('type') for event in self.events]}"
                )

            current = self.run_id()

            if current is not None:
                pending = self.store().list_approvals(current, status="pending")

                if pending:
                    return pending[0]

            time.sleep(0.02)

        raise AssertionError("等不到 pending 审批")

    def start(self, name: str, arguments: str):
        stream = tool_stream(name, arguments)
        final = iter([chunk(text="好的", finish="stop")])

        model = patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=[stream, final],
        )
        patches = [
            patch.object(agent, "init_db"),
            patch.object(agent, "_knowledge_context", return_value=None),
            model,
        ]

        for item in patches:
            item.start()
            self.addCleanup(item.stop)

        pool = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(pool.shutdown, wait=False)

        self.future = pool.submit(
            copy_context().run,
            run_chat_stream,
            "跑一次需要审批的工具",
            emit=self.events.append,
            cancel_token=self.token,
        )

        return self.future

    def trajectory(self) -> list[tuple[str, str]]:
        return [
            (event["event_type"], event["status"])
            for event in self.store().list_events(self.run_id())
        ]

    # ------------------------------------------------------------------
    # 1. 批准之前没有 exec
    # ------------------------------------------------------------------

    def test_pending_approval_creates_no_exec(self):
        future = self.start(
            "write_file", '{"path": "notes/pending.txt", "content": "x"}'
        )

        approval = self.wait_pending()

        self.assertEqual(approval["status"], "pending")
        self.assertEqual(approval["tool_name"], "write_file")
        self.assertIn("notes/pending.txt", approval["arguments_preview"])
        self.assertEqual(self.exec_rows(), [])

        # 拒绝让 run 继续走完
        self.store().decide_approval(
            approval["id"], decision="reject", decided_by="alice"
        )
        future.result(timeout=30)

        self.assertEqual(self.exec_rows(), [])

        step = next(
            step
            for step in self.store()
            .get_run(self.run_id(), owner_id="alice")["turns"][0]["steps"]
            if step["kind"] == "tool"
        )
        self.assertEqual(step["status"], "denied")

        self.assertEqual(
            self.trajectory(),
            [
                ("tool_approval_requested", "pending"),
                ("tool_approval_decided", "rejected"),
            ],
        )
        self.assertIn("tool_denied", [event["type"] for event in self.events])

    # ------------------------------------------------------------------
    # 2. 批准后恰好执行一次
    # ------------------------------------------------------------------

    def test_approved_executes_exactly_once(self):
        future = self.start(
            "write_file", '{"path": "notes/approved.txt", "content": "ok"}'
        )

        approval = self.wait_pending()
        self.store().decide_approval(
            approval["id"], decision="approve", decided_by="alice"
        )

        future.result(timeout=60)

        rows = self.exec_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "succeeded")

        self.assertEqual(
            self.trajectory(),
            [
                ("tool_approval_requested", "pending"),
                ("tool_approval_decided", "approved"),
                ("exec_started", "running"),
                ("exec_finished", "succeeded"),
            ],
        )

        approval_events = [
            event for event in self.events if event["type"] == "approval_required"
        ]
        self.assertEqual(len(approval_events), 1)
        self.assertEqual(approval_events[0]["approval_id"], approval["id"])

    # ------------------------------------------------------------------
    # 3. 只能决定一次；别人不能决定
    # ------------------------------------------------------------------

    def test_duplicate_decision_is_rejected(self):
        future = self.start(
            "write_file", '{"path": "notes/dup.txt", "content": "x"}'
        )

        approval = self.wait_pending()
        store = self.store()
        store.decide_approval(
            approval["id"], decision="reject", decided_by="alice"
        )
        future.result(timeout=30)

        with self.assertRaises(ValueError):
            store.decide_approval(
                approval["id"], decision="approve", decided_by="alice"
            )

    def test_other_user_cannot_decide(self):
        future = self.start(
            "write_file", '{"path": "notes/other.txt", "content": "x"}'
        )

        approval = self.wait_pending()

        with self.assertRaises(KeyError):
            approvals.decide_for_request(
                self.store(),
                request_id=self.request_id,
                approval_id=approval["id"],
                user_id="bob",
                decision="approve",
            )

        # 收尾：合法用户拒绝，避免线程挂住
        self.store().decide_approval(
            approval["id"], decision="reject", decided_by="alice"
        )
        future.result(timeout=30)

    # ------------------------------------------------------------------
    # 4. 超时自动过期
    # ------------------------------------------------------------------

    def test_timeout_expires_without_exec(self):
        with patch.dict(
            "os.environ", {"PERSONAL_AGENT_APPROVAL_TIMEOUT_SECONDS": "0.5"}
        ):
            future = self.start(
                "write_file", '{"path": "notes/timeout.txt", "content": "x"}'
            )
            future.result(timeout=30)

        self.assertEqual(self.exec_rows(), [])

        records = self.store().list_approvals(self.run_id())
        self.assertEqual([item["status"] for item in records], ["expired"])

        self.assertEqual(
            self.trajectory(),
            [
                ("tool_approval_requested", "pending"),
                ("tool_approval_decided", "expired"),
            ],
        )

    # ------------------------------------------------------------------
    # 5. run 取消 → pending 审批过期
    # ------------------------------------------------------------------

    def test_cancel_expires_pending_approval(self):
        future = self.start(
            "write_file", '{"path": "notes/cancel.txt", "content": "x"}'
        )

        approval = self.wait_pending()
        self.token.cancel()

        with self.assertRaises(ChatCancelled):
            future.result(timeout=30)

        self.assertEqual(self.exec_rows(), [])

        self.assertEqual(
            self.store().get_approval(approval["id"])["status"], "expired"
        )
        self.assertEqual(
            self.store().get_run(self.run_id(), owner_id="alice")["status"],
            "cancelled",
        )

    # ------------------------------------------------------------------
    # 6. 参数被换过 → 拒绝执行
    # ------------------------------------------------------------------

    def test_changed_arguments_are_rejected(self):
        future = self.start(
            "write_file", '{"path": "notes/digest.txt", "content": "x"}'
        )

        approval = self.wait_pending()

        with self.assertRaises(ToolDenied):
            approvals.verify_digest(
                approval, {"path": "notes/other.txt", "content": "x"}
            )

        self.store().decide_approval(
            approval["id"], decision="reject", decided_by="alice"
        )
        future.result(timeout=30)


class ApprovalApiTest(unittest.TestCase):
    """HTTP 层：路由、归属与 409。

    legacy 聊天记录读取（``_read``）用替身，这里只测审批路由本身。
    """

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)

        self.store = RunStore()
        self.run = self.store.create_run(
            owner_id="alice", request_id="d13_api_request"
        )
        turn = self.store.create_turn(self.run["id"], user_message="审批")
        step = self.store.add_step(
            turn["id"], kind="tool", actor="assistant", content="write_file"
        )
        self.approval = self.store.create_approval(
            self.run["id"],
            step_id=step["id"],
            tool_name="write_file",
            arguments={"path": "notes/x.txt", "content": "x"},
            preview="write_file {...}",
        )

        self.overrides = app.dependency_overrides.copy()
        self.addCleanup(self._restore_overrides)

        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def _restore_overrides(self):
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self.overrides)

    def _as(self, user: str):
        app.dependency_overrides[get_current_user] = lambda: {
            "id": user,
            "role": "member",
            "username": user,
        }

    def decide(self, user="alice", decision="approve"):
        self._as(user)

        with patch.object(chat_api, "_read", return_value={"status": "processing"}):
            return self.client.post(
                f"/chat/requests/{self.run['request_id']}"
                f"/approvals/{self.approval['id']}",
                json={"decision": decision},
            )

    def test_owner_can_approve(self):
        response = self.decide()

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["status"], "approved")
        self.assertEqual(
            self.store.get_approval(self.approval["id"])["status"], "approved"
        )

    def test_other_user_gets_404(self):
        response = self.decide(user="bob")

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(
            self.store.get_approval(self.approval["id"])["status"], "pending"
        )

    def test_second_decision_is_409(self):
        self.assertEqual(self.decide().status_code, 202)

        response = self.decide(decision="reject")

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["status"], "conflict")

    def test_list_pending(self):
        self._as("alice")

        with patch.object(chat_api, "_read", return_value={"status": "processing"}):
            response = self.client.get(
                f"/chat/requests/{self.run['request_id']}/approvals"
            )

        self.assertEqual(response.status_code, 200, response.text)
        ids = [item["approval_id"] for item in response.json()["approvals"]]
        self.assertEqual(ids, [self.approval["id"]])


if __name__ == "__main__":
    unittest.main()
