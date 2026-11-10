"""Qwen 运行目录监听测试。"""

import hashlib
import io
import json
import tempfile
import unittest
import uuid

from contextlib import redirect_stdout
from pathlib import Path

import scripts.qwen_watch as watcher_cli
from src.collab.qwen_watcher import scan_once
from src.collab.registry import ExternalTaskRegistry


class WatcherCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.runs = self.root / "runs"
        self.runs.mkdir()
        self.registry = ExternalTaskRegistry(self.root / "registry.db")

    def make_run(self, state="completed_unverified", write_manifest=True):
        run_id = str(uuid.uuid4())
        workspace = self.runs / run_id / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "out.txt").write_text("hello", encoding="utf-8")
        data = (workspace / "out.txt").read_bytes()

        if write_manifest:
            (self.runs / run_id / "manifest.json").write_text(
                json.dumps({
                    "run_id": run_id,
                    "artifacts": [{
                        "path": "out.txt",
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "bytes": len(data),
                    }],
                }),
                encoding="utf-8",
            )

        (self.runs / run_id / "state.json").write_text(
            json.dumps({"run_id": run_id, "state": state}),
            encoding="utf-8",
        )

        return run_id

    def test_discovers_and_finalizes(self):
        run_id = self.make_run()

        report = scan_once(root=self.runs, registry=self.registry)

        self.assertEqual(len(report["processed"]), 1)
        self.assertEqual(report["processed"][0]["state"], "succeeded")
        self.assertEqual(self.registry.get("qwen-cli", run_id).state, "succeeded")

    def test_skips_running(self):
        self.make_run(state="running")

        report = scan_once(root=self.runs, registry=self.registry)

        self.assertEqual(report["processed"], [])
        self.assertEqual(report["skipped"][0]["reason"], "state=running")

    def test_second_scan_skips_finalized(self):
        run_id = self.make_run()

        first = scan_once(root=self.runs, registry=self.registry)
        second = scan_once(root=self.runs, registry=self.registry)

        self.assertEqual(len(first["processed"]), 1)
        self.assertEqual(second["processed"], [])
        self.assertEqual(second["skipped"][0]["reason"], "registry=succeeded")

    def test_bad_run_does_not_block_others(self):
        broken = self.make_run(write_manifest=False)
        good = self.make_run()

        report = scan_once(root=self.runs, registry=self.registry)

        self.assertEqual(len(report["processed"]), 1)
        self.assertEqual(report["processed"][0]["run_id"], good)
        self.assertEqual(report["errors"][0]["run_id"], broken)

    def test_non_uuid_directory_ignored(self):
        self.make_run()
        (self.runs / "not-a-uuid").mkdir()
        (self.runs / "not-a-uuid" / "state.json").write_text('{"state":"completed_unverified"}', encoding="utf-8")

        report = scan_once(root=self.runs, registry=self.registry)

        self.assertEqual(len(report["processed"]), 1)

    def test_notification_only_once(self):
        calls = []
        self.make_run()

        scan_once(root=self.runs, registry=self.registry, sender=lambda text: calls.append(text))
        scan_once(root=self.runs, registry=self.registry, sender=lambda text: calls.append(text))

        self.assertEqual(len(calls), 1)

    def test_timeout_state_skipped(self):
        self.make_run(state="timeout")

        report = scan_once(root=self.runs, registry=self.registry)

        self.assertEqual(report["processed"], [])

    def test_cli_once(self):
        self.make_run()
        output = io.StringIO()

        with redirect_stdout(output):
            code = watcher_cli.main([
                "--root", str(self.runs),
                "--db", str(self.root / "cli.db"),
            ])

        payload = json.loads(output.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(len(payload["processed"]), 1)


if __name__ == "__main__":
    unittest.main()
