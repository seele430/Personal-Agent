"""QQ 同任务继续：run 解析与 relay 调用测试（全部 mock）。"""

import json
import sqlite3

from contextlib import closing
import subprocess
import tempfile
import unittest
import uuid

from pathlib import Path
from unittest.mock import patch

from src.qq.collab_continue import ContinueError, continue_run, resolve_run


class ContinueCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.db = Path(self._temporary.name) / "collab.db"

        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("CREATE TABLE collab_runs (run_id TEXT PRIMARY KEY, state TEXT NOT NULL)")
            connection.commit()

    def add(self, state="succeeded"):
        run_id = str(uuid.uuid4())

        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("INSERT INTO collab_runs VALUES (?,?)", (run_id, state))
            connection.commit()

        return run_id

    def test_missing(self):
        with self.assertRaises(ContinueError) as caught:
            resolve_run("", path=self.db)

        self.assertEqual(caught.exception.code, "missing")

    def test_invalid(self):
        with self.assertRaises(ContinueError) as caught:
            resolve_run("not a run id!", path=self.db)

        self.assertEqual(caught.exception.code, "invalid")

    def test_not_found(self):
        with self.assertRaises(ContinueError) as caught:
            resolve_run("abcdef12", path=self.db)

        self.assertEqual(caught.exception.code, "not_found")

    def test_full_uuid(self):
        run_id = self.add()

        self.assertEqual(resolve_run(run_id, path=self.db)[0], run_id)

    def test_unique_prefix(self):
        run_id = self.add()

        self.assertEqual(resolve_run(run_id[:8], path=self.db)[0], run_id)

    def test_ambiguous_prefix(self):
        self.add()
        second = str(uuid.uuid4())
        shared = second[:8]

        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("DELETE FROM collab_runs")
            connection.execute("INSERT INTO collab_runs VALUES (?,?)", (shared + "-0000-0000-0000-000000000000", "succeeded"))
            connection.execute("INSERT INTO collab_runs VALUES (?,?)", (shared + "-1111-1111-1111-111111111111", "succeeded"))
            connection.commit()

        with self.assertRaises(ContinueError) as caught:
            resolve_run(shared, path=self.db)

        self.assertEqual(caught.exception.code, "ambiguous")

    def test_not_succeeded(self):
        run_id = self.add("failed")

        with self.assertRaises(ContinueError) as caught:
            resolve_run(run_id, path=self.db)

        self.assertEqual(caught.exception.code, "not_succeeded")

    def test_continue_success(self):
        run_id = self.add()
        payload = {"run_id": run_id, "revision": 2, "state": "succeeded", "artifacts": ["a.txt"]}

        with patch(
            "src.qq.collab_continue.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), ""),
        ) as mocked:
            result = continue_run(run_id, "再补一段", path=self.db)

        self.assertEqual(result["revision"], 2)
        self.assertIn("--message", mocked.call_args.args[0])

    def test_continue_timeout(self):
        run_id = self.add()

        with patch(
            "src.qq.collab_continue.subprocess.run",
            side_effect=subprocess.TimeoutExpired("relay", 1),
        ):
            with self.assertRaises(ContinueError) as caught:
                continue_run(run_id, "x", path=self.db)

        self.assertEqual(caught.exception.code, "timeout")

    def test_continue_relay_error(self):
        run_id = self.add()

        with patch(
            "src.qq.collab_continue.subprocess.run",
            return_value=subprocess.CompletedProcess([], 2, json.dumps({"error": "run 不存在"}), ""),
        ):
            with self.assertRaises(ContinueError) as caught:
                continue_run(run_id, "x", path=self.db)

        self.assertEqual(caught.exception.code, "relay_failed")


if __name__ == "__main__":
    unittest.main()
