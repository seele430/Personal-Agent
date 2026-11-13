"""外层 CLI 测试：prompt 不进 argv、prompt-file、list 汇总。"""

import io
import json
import tempfile
import unittest

from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import scripts.qwen_task as cli
from src.collab.qwen_runner import QwenRunner


class CliCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def capture_run(self, argv, stdin_text=""):
        captured = {}

        def fake_execute(self_runner, spec):
            captured["spec"] = spec
            captured["command"] = self_runner.build_command(spec)

            return {"state": "completed_unverified", "run_id": spec.run_id}

        output = io.StringIO()

        with patch("scripts.qwen_task.QwenRunner.execute", fake_execute), patch(
            "sys.stdin", io.StringIO(stdin_text)
        ), redirect_stdout(output):
            code = cli.main(argv + ["--root", str(self.root)])

        return code, captured

    def test_stdin_prompt_not_in_argv(self):
        code, captured = self.capture_run(
            ["run", "--model", "qwen3.8-flash"], stdin_text="TOP-SECRET-PROMPT"
        )

        self.assertEqual(code, 0)
        self.assertEqual(captured["spec"].prompt, "TOP-SECRET-PROMPT")
        self.assertNotIn("TOP-SECRET-PROMPT", captured["command"])

    def test_prompt_file(self):
        prompt_file = self.root / "prompt.txt"
        prompt_file.write_text("FROM-FILE", encoding="utf-8")

        code, captured = self.capture_run(
            ["run", "--model", "qwen3.8-flash", "--prompt-file", str(prompt_file)]
        )

        self.assertEqual(captured["spec"].prompt, "FROM-FILE")
        self.assertNotIn("FROM-FILE", captured["command"])

    def test_list_reports_usage_and_null_totals(self):
        run_a = self.root / "55555555-5555-5555-5555-555555555555"
        run_b = self.root / "66666666-6666-6666-6666-666666666666"
        run_a.mkdir()
        run_b.mkdir()
        (run_a / "state.json").write_text(
            json.dumps({
                "run_id": run_a.name,
                "state": "completed_unverified",
                "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
                "duration_ms": 100,
            }),
            encoding="utf-8",
        )
        (run_b / "state.json").write_text(
            json.dumps({
                "run_id": run_b.name,
                "state": "failed",
                "usage": {"input_tokens": None, "output_tokens": None, "total_tokens": None},
                "reason": {"code": "provider_error"},
                "duration_ms": 50,
            }),
            encoding="utf-8",
        )
        output = io.StringIO()

        with redirect_stdout(output):
            code = cli.main(["list", "--root", str(self.root)])

        payload = json.loads(output.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(len(payload["runs"]), 2)
        self.assertFalse(payload["totals"]["billed_known"])
        self.assertEqual(payload["totals"]["input_tokens"], 10)


if __name__ == "__main__":
    unittest.main()
