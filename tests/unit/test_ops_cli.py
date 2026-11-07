"""运维 CLI 测试：时间线输出、生命周期预演/执行/确认。"""

import io
import sqlite3
import sys
import tempfile
import unittest

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, r"C:\Users\123\Personal Agent")

import scripts.activity_timeline as timeline_cli  # noqa: E402
import scripts.lifecycle_report as lifecycle_cli  # noqa: E402


def run(main, argv):
    buffer = io.StringIO()

    with redirect_stdout(buffer):
        code = main(argv)

    return code, buffer.getvalue()


class OpsCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.base = Path(self._temporary.name)
        self.addCleanup(self._close)

    def _close(self):
        pass

    def make_spans(self, rows):
        connection = sqlite3.connect(self.base / "traces.db")

        with connection:
            connection.execute(
                "CREATE TABLE spans (span_id TEXT, name TEXT, status TEXT, "
                "attrs TEXT, created_at TEXT)"
            )
            connection.executemany(
                "INSERT INTO spans VALUES (?,?,?,?,?)",
                [(span_id, "llm.generate", "ok", "{}", ts) for span_id, ts in rows],
            )

        connection.close()

    def make_execs(self, rows):
        connection = sqlite3.connect(self.base / "platform.db")

        with connection:
            connection.execute(
                "CREATE TABLE platform_execs (id TEXT, run_id TEXT, state TEXT, created_at TEXT)"
            )
            connection.executemany(
                "INSERT INTO platform_execs VALUES (?,?,?,?)",
                [(f"e{i}", f"r{i}", state, ts) for i, (state, ts) in enumerate(rows)],
            )

        connection.close()


class TestTimelineCli(OpsCase):
    def test_empty_dir(self):
        code, output = run(timeline_cli.main, ["--data-dir", str(self.base)])

        self.assertEqual(code, 0)
        self.assertIn("暂无活动记录", output)

    def test_prints_events(self):
        self.make_spans([("s1", "2026-11-01T10:00:00")])

        code, output = run(timeline_cli.main, ["--data-dir", str(self.base)])

        self.assertEqual(code, 0)
        self.assertIn("llm.generate", output)

    def test_merges_sources(self):
        self.make_spans([("s1", "2026-11-01T10:00:00")])
        self.make_execs([("succeeded", "2026-11-01T11:00:00")])

        code, output = run(timeline_cli.main, ["--data-dir", str(self.base)])

        self.assertIn("span", output)
        self.assertIn("exec", output)

    def test_json_output(self):
        self.make_spans([("s1", "2026-11-01T10:00:00")])

        code, output = run(
            timeline_cli.main, ["--data-dir", str(self.base), "--json"]
        )

        self.assertEqual(code, 0)
        self.assertIn('"count": 1', output)

    def test_bad_limit(self):
        code, output = run(
            timeline_cli.main, ["--data-dir", str(self.base), "--limit", "0"]
        )

        self.assertEqual(code, 2)
        self.assertIn("参数错误", output)

    def test_limit_applied(self):
        self.make_spans(
            [("s1", "2026-11-01T10:00:00"), ("s2", "2026-11-02T10:00:00")]
        )

        code, output = run(
            timeline_cli.main, ["--data-dir", str(self.base), "--limit", "1"]
        )

        self.assertIn("共 1 条", output)


class TestLifecycleCli(OpsCase):
    def count(self, path):
        connection = sqlite3.connect(path)

        try:
            return connection.execute("SELECT COUNT(*) FROM spans").fetchone()[0]

        finally:
            connection.close()

    def old(self, days):
        return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    def test_missing_dir(self):
        code, output = run(
            lifecycle_cli.main, ["--data-dir", str(self.base / "nope")]
        )

        self.assertEqual(code, 3)

    def test_dry_run_does_not_delete(self):
        self.make_spans([("s1", self.old(100)), ("s2", self.old(90))])

        code, output = run(
            lifecycle_cli.main,
            ["--data-dir", str(self.base), "--days", "30", "--keep-last", "0"],
        )

        self.assertEqual(code, 0)
        self.assertIn("预演", output)
        self.assertEqual(self.count(self.base / "traces.db"), 2)

    def test_apply_without_confirm_refused(self):
        self.make_spans([("s1", self.old(100))])

        code, output = run(
            lifecycle_cli.main,
            ["--data-dir", str(self.base), "--apply"],
        )

        self.assertEqual(code, 2)
        self.assertIn("--confirm", output)
        self.assertEqual(self.count(self.base / "traces.db"), 1)

    def test_apply_with_confirm_deletes(self):
        self.make_spans([("s1", self.old(100)), ("s2", self.old(90))])

        code, output = run(
            lifecycle_cli.main,
            [
                "--data-dir", str(self.base), "--days", "30",
                "--keep-last", "0", "--apply", "--confirm",
            ],
        )

        self.assertEqual(code, 0)
        self.assertIn("已执行", output)
        self.assertEqual(self.count(self.base / "traces.db"), 0)

    def test_json_output(self):
        self.make_spans([("s1", self.old(100))])

        code, output = run(
            lifecycle_cli.main,
            ["--data-dir", str(self.base), "--days", "30", "--keep-last", "0", "--json"],
        )

        self.assertEqual(code, 0)
        self.assertIn('"total"', output)

    def test_invalid_days(self):
        code, output = run(
            lifecycle_cli.main, ["--data-dir", str(self.base), "--days", "0"]
        )

        self.assertEqual(code, 2)

    def test_keep_last_protects(self):
        self.make_spans([("s1", self.old(100)), ("s2", self.old(90))])

        code, _ = run(
            lifecycle_cli.main,
            [
                "--data-dir", str(self.base), "--days", "30",
                "--keep-last", "5", "--apply", "--confirm",
            ],
        )

        self.assertEqual(code, 0)
        self.assertEqual(self.count(self.base / "traces.db"), 2)


if __name__ == "__main__":
    unittest.main()
