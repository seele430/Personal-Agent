"""文件生命周期测试：预演不删、确认才删、后缀/链接边界、CLI 护栏。"""

import io
import os
import sys
import tempfile
import unittest

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, r"C:\Users\123\Personal Agent")

from src.lifecycle.files import (  # noqa: E402
    FileLifecycleError,
    plan_files,
    purge_files,
)
import scripts.lifecycle_files as cli  # noqa: E402


NOW = datetime(2026, 11, 8, tzinfo=timezone.utc)


def touch(path: Path, *, days_old: float, content: bytes = b"x") -> Path:
    path.write_bytes(content)
    stamp = (NOW - timedelta(days=days_old)).timestamp()
    os.utime(path, (stamp, stamp))

    return path


class FileCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def make(self, name, *, days_old, content=b"x"):
        return touch(self.root / name, days_old=days_old, content=content)


class TestPlan(FileCase):
    def test_old_file_is_candidate(self):
        self.make("old.md", days_old=40)

        plan = plan_files(self.root, days=30, now=NOW)

        self.assertEqual(plan.count, 1)
        self.assertTrue(plan.candidates[0].path.name == "old.md")

    def test_new_file_protected(self):
        self.make("new.md", days_old=5)

        self.assertEqual(plan_files(self.root, days=30, now=NOW).count, 0)

    def test_suffix_filter(self):
        self.make("old.md", days_old=40)
        self.make("old.exe", days_old=40)

        plan = plan_files(self.root, days=30, now=NOW)

        self.assertEqual(plan.count, 1)
        self.assertEqual(plan.candidates[0].path.suffix, ".md")

    def test_custom_suffix(self):
        self.make("old.log", days_old=40)

        plan = plan_files(self.root, days=30, suffixes=(".log",), now=NOW)

        self.assertEqual(plan.count, 1)

    def test_missing_dir_empty_plan(self):
        plan = plan_files(self.root / "nope", days=30, now=NOW)

        self.assertEqual(plan.count, 0)

    def test_invalid_days(self):
        with self.assertRaises(FileLifecycleError):
            plan_files(self.root, days=0)

    def test_empty_suffixes(self):
        with self.assertRaises(FileLifecycleError):
            plan_files(self.root, days=30, suffixes=())

    def test_plan_bytes(self):
        self.make("old.md", days_old=40, content=b"12345")

        self.assertEqual(plan_files(self.root, days=30, now=NOW).bytes, 5)

    def test_symlink_skipped(self):
        target = self.make("real.md", days_old=40)
        link = self.root / "link.md"

        try:
            link.symlink_to(target)

        except (OSError, NotImplementedError):
            self.skipTest("当前环境不支持创建符号链接")

        plan = plan_files(self.root, days=30, now=NOW)

        self.assertGreaterEqual(plan.skipped_links, 1)
        self.assertTrue(target.exists())


class TestPurge(FileCase):
    def test_requires_confirm(self):
        self.make("old.md", days_old=40)
        plan = plan_files(self.root, days=30, now=NOW)

        with self.assertRaises(FileLifecycleError):
            purge_files(plan)

        self.assertTrue((self.root / "old.md").exists())

    def test_deletes_only_candidates(self):
        self.make("old.md", days_old=40)
        self.make("new.md", days_old=1)
        plan = plan_files(self.root, days=30, now=NOW)

        result = purge_files(plan, confirm=True)

        self.assertEqual(result["deleted"], 1)
        self.assertFalse((self.root / "old.md").exists())
        self.assertTrue((self.root / "new.md").exists())

    def test_freed_bytes(self):
        self.make("old.md", days_old=40, content=b"123456")
        plan = plan_files(self.root, days=30, now=NOW)

        self.assertEqual(purge_files(plan, confirm=True)["freed_bytes"], 6)


class TestCli(FileCase):
    def run_cli(self, argv):
        buffer = io.StringIO()

        with redirect_stdout(buffer):
            code = cli.main(argv)

        return code, buffer.getvalue()

    def test_dry_run(self):
        self.make("old.md", days_old=40)

        code, output = self.run_cli(["--root", str(self.root), "--days", "30"])

        self.assertEqual(code, 0)
        self.assertIn("预演", output)
        self.assertTrue((self.root / "old.md").exists())

    def test_apply_without_confirm(self):
        self.make("old.md", days_old=40)

        code, output = self.run_cli(["--root", str(self.root), "--apply"])

        self.assertEqual(code, 2)
        self.assertIn("--confirm", output)
        self.assertTrue((self.root / "old.md").exists())

    def test_apply_with_confirm(self):
        self.make("old.md", days_old=40)

        code, output = self.run_cli(
            ["--root", str(self.root), "--days", "30", "--apply", "--confirm"]
        )

        self.assertEqual(code, 0)
        self.assertIn("已执行", output)
        self.assertFalse((self.root / "old.md").exists())

    def test_missing_root(self):
        code, _ = self.run_cli(["--root", str(self.root / "nope")])

        self.assertEqual(code, 3)

    def test_json_output(self):
        self.make("old.md", days_old=40)

        code, output = self.run_cli(
            ["--root", str(self.root), "--days", "30", "--json"]
        )

        self.assertEqual(code, 0)
        self.assertIn('"count": 1', output)


if __name__ == "__main__":
    unittest.main()
