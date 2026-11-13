"""外部任务 CLI 测试：登记幂等、列表过滤、查询缺失、参数校验。"""

import io
import json
import sys
import tempfile
import unittest

from contextlib import redirect_stdout
from pathlib import Path


sys.path.insert(0, r"C:\Users\123\Personal Agent")

import scripts.collab_tasks as cli  # noqa: E402


def run(argv):
    buffer = io.StringIO()

    with redirect_stdout(buffer):
        code = cli.main(argv)

    return code, buffer.getvalue()


class CollabCliCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.db = str(Path(self._temporary.name) / "tasks.db")

    def register(self, **overrides):
        argv = [
            "--db", self.db, "register",
            "--provider", overrides.get("provider", "kimi"),
            "--external-id", overrides.get("external_id", "K-1"),
            "--run-id", overrides.get("run_id", "r1"),
        ]

        if "state" in overrides:
            argv += ["--state", overrides["state"]]

        for artifact in overrides.get("artifacts", []):
            argv += ["--artifact", artifact]

        argv += overrides.get("extra", [])

        return run(argv)


class TestRegister(CollabCliCase):
    def test_registers(self):
        code, output = self.register()

        self.assertEqual(code, 0)
        self.assertIn("kimi/K-1", output)

    def test_idempotent(self):
        self.register()
        self.register(run_id="r2", state="running")

        code, output = run(["--db", self.db, "list"])

        self.assertEqual(code, 0)
        self.assertEqual(output.count("kimi/K-1"), 1)
        self.assertIn("running", output)

    def test_invalid_state(self):
        code, output = self.register(state="done")

        self.assertEqual(code, 2)
        self.assertIn("参数错误", output)

    def test_artifacts_recorded(self):
        self.register(artifacts=["a.md", "b.png"])

        code, output = run(["--db", self.db, "list"])

        self.assertIn("a.md,b.png", output)

    def test_json_output(self):
        code, output = self.register(extra=["--json"])

        self.assertEqual(code, 0)
        self.assertIn('"external_id": "K-1"', output)


class TestList(CollabCliCase):
    def test_empty(self):
        code, output = run(["--db", self.db, "list"])

        self.assertEqual(code, 0)
        self.assertIn("暂无外部任务", output)

    def test_provider_filter(self):
        self.register(provider="kimi", external_id="K-1")
        self.register(provider="dsh", external_id="D-1")

        code, output = run(["--db", self.db, "list", "--provider", "dsh"])

        self.assertIn("dsh/D-1", output)
        self.assertNotIn("kimi/K-1", output)

    def test_limit_validation(self):
        code, output = run(["--db", self.db, "list", "--limit", "0"])

        self.assertEqual(code, 2)
        self.assertIn("参数错误", output)

    def test_limit_applied(self):
        self.register(external_id="K-1")
        self.register(external_id="K-2")

        code, output = run(["--db", self.db, "list", "--limit", "1"])

        self.assertEqual(output.count("kimi/"), 1)

    def test_json_list(self):
        self.register()

        code, output = run(["--db", self.db, "list", "--json"])

        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(output)), 1)


class TestShow(CollabCliCase):
    def test_show_existing(self):
        self.register()

        code, output = run(
            ["--db", self.db, "show", "--provider", "kimi", "--external-id", "K-1"]
        )

        self.assertEqual(code, 0)
        self.assertIn("K-1", output)

    def test_show_missing_exits_3(self):
        code, output = run(
            ["--db", self.db, "show", "--provider", "kimi", "--external-id", "nope"]
        )

        self.assertEqual(code, 3)
        self.assertIn("任务不存在", output)

    def test_show_json(self):
        self.register()

        code, output = run(
            [
                "--db", self.db, "show", "--provider", "kimi",
                "--external-id", "K-1", "--json",
            ]
        )

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["state"], "submitted")


if __name__ == "__main__":
    unittest.main()
