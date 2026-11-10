"""Runner 状态机测试（mock 进程，不调用真实模型）。"""

import json
import subprocess
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from src.collab.qwen_runner import QwenRunner, RunSpec, status_of


EVIDENCE = Path(
    r"C:\Users\123\Documents\deepseek-harness\default-workspace\pa-v14a2-step0-20261109"
)


def fixture(name: str) -> str:
    return (EVIDENCE / name).read_text(encoding="utf-8")


class FakeProcess:
    def __init__(self, stdout: str, code: int = 0, timeout: bool = False, reaped: bool = True):
        self.pid = 4242
        self._stdout = stdout
        self._code = code
        self._timeout = timeout
        self._reaped = reaped
        self._raised = False
        self.returncode = None

    def communicate(self, input=None, timeout=None):
        if self._timeout and not self._raised:
            self._raised = True
            raise subprocess.TimeoutExpired("qwen", timeout or 1)

        self.returncode = self._code if self._reaped else None

        return self._stdout, ""

    def poll(self):
        return self.returncode


def spec(**overrides):
    base = dict(
        run_id="run-1",
        provider_session_id="11111111-1111-1111-1111-111111111111",
        prompt="say ok",
        model="qwen3.8-flash",
        approval_mode="plan",
        expected_artifacts={},
        max_wall_time="30s",
        timeout_seconds=5,
    )
    base.update(overrides)

    return RunSpec(**base)


class RunnerCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.runner = QwenRunner(root=self.root)

    def execute(self, process, run_spec=None):
        with patch("src.collab.qwen_runner.subprocess.Popen", return_value=process), patch(
            "src.collab.qwen_runner.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")
        ):
            return self.runner.execute(run_spec or spec())

    def success_fixture(self):
        data = json.loads(fixture("success-schema.json"))
        data[0]["session_id"] = "11111111-1111-1111-1111-111111111111"
        data[-1]["session_id"] = "11111111-1111-1111-1111-111111111111"

        return json.dumps(data)

    def test_success_becomes_completed_unverified(self):
        state = self.execute(FakeProcess(self.success_fixture()))

        self.assertEqual(state["state"], "completed_unverified")
        self.assertEqual(state["observed_model"], "qwen3.8-flash")
        self.assertTrue((Path(self.root) / "run-1" / "state.json").exists())

    def test_nonzero_exit_fails(self):
        state = self.execute(FakeProcess(self.success_fixture(), code=1))

        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["reason"]["code"], "provider_error")

    def test_model_fallback_fails(self):
        data = json.loads(fixture("model-fallback-schema.json"))
        data[0]["session_id"] = "11111111-1111-1111-1111-111111111111"
        data[-1]["session_id"] = "11111111-1111-1111-1111-111111111111"
        state = self.execute(FakeProcess(json.dumps(data)))

        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["reason"]["code"], "model_mismatch")

    def test_session_mismatch_fails(self):
        data = json.loads(self.success_fixture())
        data[-1]["session_id"] = "22222222-2222-2222-2222-222222222222"

        state = self.execute(FakeProcess(json.dumps(data)))

        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["reason"]["code"], "session_mismatch")

    def test_missing_result_fails(self):
        data = [event for event in json.loads(self.success_fixture()) if event.get("type") != "result"]

        state = self.execute(FakeProcess(json.dumps(data)))

        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["reason"]["code"], "missing_result")

    def test_timeout_reaped_is_timeout(self):
        state = self.execute(FakeProcess("", timeout=True, reaped=True))

        self.assertEqual(state["state"], "timeout")
        self.assertTrue(state["cleanup"]["process_reaped"])

    def test_timeout_not_reaped_is_unknown(self):
        state = self.execute(FakeProcess("", timeout=True, reaped=False))

        self.assertEqual(state["state"], "unknown")
        self.assertEqual(state["reason"]["code"], "watchdog_timeout")

    def test_missing_artifact_fails(self):
        state = self.execute(
            FakeProcess(self.success_fixture()),
            run_spec=spec(expected_artifacts={"out.txt": False}),
        )

        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["reason"]["code"], "artifact_missing")

    def test_command_and_env(self):
        command = self.runner.build_command(spec())
        env = self.runner.build_env()

        self.assertIn("--session-id", command)
        self.assertIn("--output-format", command)
        self.assertIn("--max-wall-time", command)
        self.assertNotIn("--no-browser", command)
        self.assertEqual(env["NO_BROWSER"], "1")
        self.assertNotIn("QWEN_API_KEY", env)

    def test_stale_running_reconciled(self):
        workspace = Path(self.root) / "stale"
        workspace.mkdir()
        (workspace / "state.json").write_text(
            json.dumps({"state": "running", "pid": 999999, "run_id": "stale"}),
            encoding="utf-8",
        )

        with patch("src.collab.qwen_runner._pid_alive", return_value=False):
            state = status_of(self.root, "stale")

        self.assertEqual(state["state"], "unknown")
        self.assertEqual(state["reason"]["code"], "stale_running")


if __name__ == "__main__":
    unittest.main()
