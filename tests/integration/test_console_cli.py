"""CLI 控制台：离线可读、只读、不改数据库。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory

from src.core.run_store import RunStore
from src.routing import Router
from src.routing.events import record_route_decision

ROOT = Path(__file__).resolve().parents[2]


class ConsoleCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)

        self.data = Path(self.temp.name)
        store = RunStore(self.data / "chat_requests.db")
        run = store.create_run(owner_id="alice")
        store.append_event(run["id"], event_type="run_started", status="running")
        record_route_decision(
            store, run["id"], Router().decide("分析这个 CSV 的缺失值")
        )

        self.db = self.data / "chat_requests.db"

    def run_cli(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "console_overview.py"),
                "--data-dir",
                str(self.data),
                "--json",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )

    def test_offline_json_and_read_only(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()

        result = self.run_cli()

        self.assertEqual(result.returncode, 0, result.stderr[-1000:])

        payload = json.loads(result.stdout)

        self.assertEqual(len(payload["routes"]), 1)
        self.assertEqual(payload["routes"][0]["intent"], "data.analysis")

        after = hashlib.sha256(self.db.read_bytes()).hexdigest()

        self.assertEqual(before, after, "CLI 不应修改数据库")

    def test_missing_platform_db_is_not_created(self):
        platform_db = self.data / "platform.db"
        self.assertFalse(platform_db.exists())

        self.run_cli()

        self.assertFalse(platform_db.exists(), "CLI 不应创建缺失的数据库")


if __name__ == "__main__":
    unittest.main()
