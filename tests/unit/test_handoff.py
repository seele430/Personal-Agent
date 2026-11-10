"""跨 Agent 工件接力测试（全 mock，无真实模型）。"""

import json
import os
import tempfile
import unittest
import uuid

from pathlib import Path

from src.collab.handoff import (
    HandoffError,
    build_task,
    export_handoff,
    load_handoff,
    run_handoff,
)
from src.collab.qwen_workspace import RunWorkspace
from src.collab.registry import ExternalTaskRegistry
from src.collab.relay import RelayRunner
from src.collab.run_store import RunStore


class MockProvider:
    name = "deepseek"

    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def submit(self, prompt, *, artifact_name, out_dir):
        self.calls += 1

        if self.fail:
            raise RuntimeError("provider down")

        Path(out_dir).mkdir(parents=True, exist_ok=True)
        path = Path(out_dir) / artifact_name
        path.write_text("child summary\n", encoding="utf-8")

        return {"artifact": str(path)}


class MockRunner:
    def __init__(self, state="succeeded", raises=False):
        self.state = state
        self.raises = raises
        self.calls = 0

    def start(self, run_id):
        self.calls += 1

        if self.raises:
            raise RuntimeError("unknown")

        return {"run_id": run_id, "state": self.state}


class HandoffCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.qwen_root = self.root / "qwen-runs"
        self.handoff_root = self.root / "handoffs"
        self.registry = ExternalTaskRegistry(self.root / "registry.db")

    def make_run(self, files=None, registry_state="succeeded", registry_run_id=None, register=True):
        run_id = str(uuid.uuid4())
        workspace = RunWorkspace(self.qwen_root, run_id).create()
        files = files or {"out.txt": "hello\n"}
        entries = []

        import hashlib

        for name, text in files.items():
            data = text.encode("utf-8")
            (workspace.workspace / name).write_bytes(data)
            entries.append({
                "path": name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            })

        workspace.manifest_path.write_text(
            json.dumps({"run_id": run_id, "artifacts": entries}), encoding="utf-8"
        )
        (self.qwen_root / run_id / "state.json").write_text(
            json.dumps({"run_id": run_id, "state": "completed_unverified"}),
            encoding="utf-8",
        )
        if register:
            self.registry.register(
                "qwen-cli",
                run_id,
                run_id=registry_run_id or run_id,
                state=registry_state,
                artifacts=tuple(item["path"] for item in entries),
            )

        return run_id, entries

    def export(self, run_id, instruction="请总结上游材料"):
        return export_handoff(
            run_id,
            instruction,
            root=self.qwen_root,
            registry=self.registry,
            out_dir=self.handoff_root,
        )

    def test_export_ok(self):
        run_id, _ = self.make_run()
        package = self.export(run_id)

        self.assertEqual(package["parent_run_id"], run_id)
        self.assertEqual(package["child_state"], "exported")
        self.assertTrue((self.handoff_root / package["handoff_id"] / "files" / "00.txt").exists())

    def test_non_qwen_rejected(self):
        run_id, entries = self.make_run(register=False)
        self.registry.register("other", run_id, run_id=run_id, state="succeeded", artifacts=("out.txt",))

        with self.assertRaises(HandoffError) as caught:
            self.export(run_id)

        self.assertEqual(caught.exception.code, "not_found")

    def test_not_succeeded_rejected(self):
        run_id, _ = self.make_run(registry_state="failed")

        with self.assertRaises(HandoffError) as caught:
            self.export(run_id)

        self.assertEqual(caught.exception.code, "not_succeeded")

    def test_identity_mismatch(self):
        run_id, _ = self.make_run(registry_run_id="other-run")

        with self.assertRaises(HandoffError) as caught:
            self.export(run_id)

        self.assertEqual(caught.exception.code, "identity_mismatch")

    def test_empty_artifacts_rejected(self):
        run_id = str(uuid.uuid4())
        RunWorkspace(self.qwen_root, run_id).create()
        (self.qwen_root / run_id / "manifest.json").write_text(
            json.dumps({"artifacts": []}), encoding="utf-8"
        )
        self.registry.register("qwen-cli", run_id, run_id=run_id, state="succeeded", artifacts=())

        with self.assertRaises(HandoffError) as caught:
            self.export(run_id)

        self.assertEqual(caught.exception.code, "no_artifacts")

    def test_missing_file_rejected(self):
        run_id, _ = self.make_run()
        (self.qwen_root / run_id / "workspace" / "out.txt").unlink()

        with self.assertRaises(HandoffError) as caught:
            self.export(run_id)

        self.assertEqual(caught.exception.code, "unsafe_path")

    def test_hash_mismatch_rejected(self):
        run_id, entries = self.make_run()
        workspace = RunWorkspace(self.qwen_root, run_id)
        entries[0]["sha256"] = "0" * 64
        workspace.manifest_path.write_text(json.dumps({"artifacts": entries}), encoding="utf-8")

        with self.assertRaises(HandoffError) as caught:
            self.export(run_id)

        self.assertEqual(caught.exception.code, "hash_mismatch")

    def test_traversal_rejected(self):
        run_id, entries = self.make_run()
        workspace = RunWorkspace(self.qwen_root, run_id)
        outside = self.qwen_root / run_id / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        entries[0]["path"] = "../outside.txt"
        workspace.manifest_path.write_text(json.dumps({"artifacts": entries}), encoding="utf-8")
        self.registry.register(
            "qwen-cli", run_id, run_id=run_id, state="succeeded", artifacts=("../outside.txt",)
        )

        with self.assertRaises(HandoffError) as caught:
            self.export(run_id)

        self.assertEqual(caught.exception.code, "unsafe_path")

    def test_symlink_rejected(self):
        run_id, entries = self.make_run()
        workspace = RunWorkspace(self.qwen_root, run_id)
        outside = self.root / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        link = workspace.workspace / "link.txt"

        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink 不可用")

        entries[0]["path"] = "link.txt"
        workspace.manifest_path.write_text(json.dumps({"artifacts": entries}), encoding="utf-8")
        self.registry.register("qwen-cli", run_id, run_id=run_id, state="succeeded", artifacts=("link.txt",))

        with self.assertRaises(HandoffError) as caught:
            self.export(run_id)

        self.assertEqual(caught.exception.code, "unsafe_path")

    def test_too_many_files_rejected(self):
        run_id, entries = self.make_run(files={f"f{i}.txt": "x" for i in range(6)})

        with self.assertRaises(HandoffError) as caught:
            self.export(run_id)

        self.assertEqual(caught.exception.code, "too_many_files")

    def test_budget_exceeded(self):
        run_id, _ = self.make_run()

        with self.assertRaises(HandoffError) as caught:
            self.export(run_id, instruction="x" * (201 * 1024))

        self.assertEqual(caught.exception.code, "budget_exceeded")

    def test_snapshot_tamper_rejected(self):
        run_id, _ = self.make_run()
        package = self.export(run_id)
        snapshot = self.handoff_root / package["handoff_id"] / "files" / "00.txt"
        snapshot.write_text("tampered\n", encoding="utf-8")

        with self.assertRaises(HandoffError) as caught:
            load_handoff(package["handoff_id"], root=self.handoff_root)

        self.assertEqual(caught.exception.code, "snapshot_tampered")

    def test_build_task_contains_provenance(self):
        run_id, _ = self.make_run()
        package = self.export(run_id)
        task = build_task(package, root=self.handoff_root)

        self.assertIn("请总结上游材料", task)
        self.assertIn(f"parent_run_id={run_id}", task)
        self.assertIn("UNTRUSTED_TEXT_DATA", task)
        self.assertIn("hello", task)

    def test_chain_validation(self):
        run_id, _ = self.make_run()
        package = self.export(run_id)
        store = RunStore(self.root / "collab.db")

        for chain in ([], ["qwen"], ["ark", "ark"], ["unknown"]):
            with self.assertRaises(HandoffError):
                run_handoff(
                    package["handoff_id"], chain,
                    handoff_root=self.handoff_root, store=store, runner=MockRunner(),
                )

    def test_e2e_mock_child_succeeded(self):
        run_id, _ = self.make_run()
        package = self.export(run_id)
        store = RunStore(self.root / "collab.db")
        provider = MockProvider()
        runner = RelayRunner(
            store=store, providers={"deepseek": provider}, artifact_root=self.root / "artifacts"
        )
        report = run_handoff(
            package["handoff_id"], ["deepseek"], handoff_root=self.handoff_root, store=store, runner=runner
        )

        self.assertEqual(report["child_state"], "succeeded")
        child = store.get(report["child_run_id"])
        self.assertEqual(child["state"], "succeeded")
        self.assertTrue((self.root / "artifacts" / report["child_run_id"] / "r1" / "result.md").exists())

    def test_duplicate_run_reuses_child(self):
        run_id, _ = self.make_run()
        package = self.export(run_id)
        runner = MockRunner()
        store = RunStore(self.root / "collab.db")
        first = run_handoff(
            package["handoff_id"], ["deepseek"], handoff_root=self.handoff_root, store=store, runner=runner
        )
        second = run_handoff(
            package["handoff_id"], ["deepseek"], handoff_root=self.handoff_root, store=store, runner=runner
        )

        self.assertTrue(second["reused"])
        self.assertEqual(first["child_run_id"], second["child_run_id"])
        self.assertEqual(runner.calls, 1)

    def test_provider_failure_keeps_child(self):
        run_id, _ = self.make_run()
        package = self.export(run_id)
        runner = MockRunner(state="failed")
        store = RunStore(self.root / "collab.db")
        report = run_handoff(
            package["handoff_id"], ["deepseek"], handoff_root=self.handoff_root, store=store, runner=runner
        )

        self.assertEqual(report["child_state"], "failed")
        self.assertTrue(report["child_run_id"])
        self.assertEqual(runner.calls, 1)

    def test_runner_exception_is_unknown(self):
        run_id, _ = self.make_run()
        package = self.export(run_id)
        runner = MockRunner(raises=True)
        store = RunStore(self.root / "collab.db")
        report = run_handoff(
            package["handoff_id"], ["deepseek"], handoff_root=self.handoff_root, store=store, runner=runner
        )

        self.assertEqual(report["child_state"], "unknown")


if __name__ == "__main__":
    unittest.main()
