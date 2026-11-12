"""DSH runner tests：事件解析、失败关闭、超时杀进程树。"""

import json
import subprocess
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from src.collab import dsh_runner


SAMPLE = "\n".join([
    json.dumps({"type": "session", "sessionId": "s1"}),
    json.dumps({"type": "status", "phase": "step_end", "turn": 1, "step": 1,
                "usage": {"inputTokens": 10, "outputTokens": 2}}),
    json.dumps({"type": "final", "sessionId": "s1",
                "reason": {"kind": "completed"}, "text": "ok"}),
])


class FakeProcess:
    def __init__(self, stdout="", stderr="", returncode=0, timeout=False):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.timeout = timeout
        self.pid = 4242
        self.raised = False
        self.killed = False

    def communicate(self, input=None, timeout=None):
        if self.timeout and not self.raised:
            self.raised = True
            raise subprocess.TimeoutExpired(cmd="dsh", timeout=timeout or 1)

        return self.stdout, self.stderr

    def kill(self):
        self.killed = True


class ParseCase(unittest.TestCase):
    def test_parse_final_text_and_usage(self):
        parsed = dsh_runner.parse_events(SAMPLE)

        self.assertEqual(parsed["text"], "ok")
        self.assertEqual(parsed["reason"], "completed")
        self.assertEqual(parsed["session_id"], "s1")
        self.assertEqual(parsed["usage"]["outputTokens"], 2)
        self.assertEqual(parsed["events"], 3)

    def test_bad_lines_counted(self):
        parsed = dsh_runner.parse_events("not json\n" + SAMPLE)

        self.assertEqual(parsed["bad_lines"], 1)
        self.assertEqual(parsed["events"], 3)

    def test_missing_final_has_empty_text(self):
        parsed = dsh_runner.parse_events(json.dumps({"type": "session", "sessionId": "s1"}))

        self.assertEqual(parsed["text"], "")
        self.assertEqual(parsed["reason"], "")


class RunCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.workspace = Path(self._temporary.name)

    def run_with(self, process, prompt="hi"):
        with patch.object(dsh_runner.subprocess, "Popen", return_value=process):
            return dsh_runner.run(prompt, self.workspace)

    def test_success(self):
        result = self.run_with(FakeProcess(stdout=SAMPLE))

        self.assertTrue(result.ok)
        self.assertEqual(result.text, "ok")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.reason, "completed")

    def test_nonzero_exit_fails(self):
        result = self.run_with(FakeProcess(stdout=SAMPLE, returncode=2))

        self.assertFalse(result.ok)
        self.assertIn("exit=2", result.error)

    def test_missing_final_fails(self):
        stdout = json.dumps({"type": "session", "sessionId": "s1"})
        result = self.run_with(FakeProcess(stdout=stdout))

        self.assertFalse(result.ok)
        self.assertIn("reason=", result.error)

    def test_timeout_kills_tree(self):
        process = FakeProcess(timeout=True)

        with patch.object(dsh_runner.subprocess, "Popen", return_value=process):
            with patch.object(dsh_runner, "_kill_tree") as kill:
                result = dsh_runner.run("hi", self.workspace, timeout=0.05)

        self.assertFalse(result.ok)
        self.assertIn("timeout", result.error)
        kill.assert_called_once()
        self.assertTrue(process.killed)

    def test_empty_prompt_rejected(self):
        result = dsh_runner.run("   ", self.workspace)

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "empty_prompt")


if __name__ == "__main__":
    unittest.main()
