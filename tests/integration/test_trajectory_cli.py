"""D14 轨迹渲染与 CLI：一条对话的 step / exec / 审批 / 事件都能看到。"""

from __future__ import annotations

import io
import json
import unittest

from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.trace_run import main
from src.core.run_store import RunStore
from src.core.trajectory_view import collect_run, render

SANDBOX_ID = "sbx_" + "a" * 32


class TrajectoryCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "chat.db"
        self.store = RunStore(self.db_path)

        self.run = self.store.create_run(
            owner_id="alice", request_id="d14_trace_request"
        )
        run_id = self.run["id"]

        turn = self.store.create_turn(run_id, user_message="五分钟后提醒我喝水")
        self.store.add_step(
            turn["id"], kind="user", actor="user",
            content="五分钟后提醒我喝水", status="succeeded",
        )
        assistant = self.store.add_step(
            turn["id"], kind="assistant", actor="assistant", content=""
        )
        tool_step = self.store.add_step(
            turn["id"], kind="tool", actor="assistant", content="create_reminder"
        )

        execution = self.store.record_exec(
            tool_step["id"], tool_name="create_reminder",
            request={"message": "喝水", "minutes": 5}, status="queued",
        )
        self.store.start_exec(execution["id"], sandbox_id=SANDBOX_ID)
        self.store.update_exec(
            execution["id"], status="succeeded", result={"reminder_id": 1}
        )
        self.store.update_step(
            assistant["id"], status="succeeded", content="提醒已创建"
        )
        self.store.update_step(
            tool_step["id"], status="succeeded", content='{"reminder_id": 1}'
        )

        self.store.append_event(run_id, event_type="run_started", status="running")
        self.store.append_event(
            run_id, event_type="exec_started", status="running",
            step_id=tool_step["id"], exec_id=execution["id"],
            sandbox_id=SANDBOX_ID,
        )
        self.store.append_event(
            run_id, event_type="exec_finished", status="succeeded",
            step_id=tool_step["id"], exec_id=execution["id"],
            sandbox_id=SANDBOX_ID,
            payload={"exit_code": 0, "cleanup": "job_object", "duration_ms": 42},
        )
        self.store.append_event(run_id, event_type="run_finished", status="succeeded")
        self.store.finish(run_id, status="succeeded")

        approval = self.store.create_approval(
            run_id,
            step_id=tool_step["id"],
            tool_name="write_file",
            arguments={"path": "notes/x.txt", "content": "x"},
            preview="write_file {...}",
        )
        self.store.decide_approval(
            approval["id"], decision="reject", decided_by="alice"
        )

        self.run_id = run_id

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------

    def test_render_shows_steps_exec_and_events(self):
        text = render(collect_run(self.store, self.run_id))

        self.assertIn(f"run {self.run_id}", text)
        self.assertIn("status=succeeded", text)
        self.assertIn("turn 1", text)
        self.assertIn("step 1", text)
        self.assertIn("create_reminder", text)
        self.assertIn("[succeeded]", text)
        self.assertIn("exec_started/running", text)
        self.assertIn("exec_finished/succeeded", text)
        self.assertIn("run_started/running", text)
        self.assertIn("run_finished/succeeded", text)
        self.assertIn("write_file", text)
        self.assertIn("rejected", text)

    def test_collect_run_is_json_serializable(self):
        data = collect_run(self.store, self.run_id)
        payload = json.dumps(data, ensure_ascii=False, default=str)

        self.assertIn("exec_finished", payload)
        self.assertEqual(data["run"]["status"], "succeeded")
        self.assertEqual(len(data["approvals"]), 1)

    # ------------------------------------------------------------------
    # CLI
    # ------------------------------------------------------------------

    def test_cli_text_output(self):
        buffer = io.StringIO()

        with redirect_stdout(buffer):
            code = main(["--db", str(self.db_path), self.run_id])

        self.assertEqual(code, 0)
        self.assertIn(f"run {self.run_id}", buffer.getvalue())

    def test_cli_json_output(self):
        buffer = io.StringIO()

        with redirect_stdout(buffer):
            code = main(["--db", str(self.db_path), "--json", self.run_id])

        self.assertEqual(code, 0)
        parsed = json.loads(buffer.getvalue())
        self.assertEqual(parsed["run"]["id"], self.run_id)
        self.assertTrue(parsed["events"])

    def test_cli_lookup_by_request_id(self):
        buffer = io.StringIO()

        with redirect_stdout(buffer):
            code = main(
                [
                    "--db", str(self.db_path),
                    "--request-id", "d14_trace_request",
                    "--owner", "alice",
                ]
            )

        self.assertEqual(code, 0)
        self.assertIn(self.run_id, buffer.getvalue())

    def test_cli_unknown_run_returns_2(self):
        stderr = io.StringIO()

        with redirect_stdout(io.StringIO()), redirect_err(stderr):
            code = main(["--db", str(self.db_path), "run_missing"])

        self.assertEqual(code, 2)
        self.assertIn("找不到", stderr.getvalue())


class redirect_err:
    """最小 stderr 重定向（contextlib 只有 redirect_stdout）。"""

    def __init__(self, target):
        self.target = target

    def __enter__(self):
        import sys

        self._original = sys.stderr
        sys.stderr = self.target
        return self.target

    def __exit__(self, *args):
        import sys

        sys.stderr = self._original


if __name__ == "__main__":
    unittest.main()
