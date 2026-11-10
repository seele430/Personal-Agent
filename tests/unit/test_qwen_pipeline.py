"""Qwen Runner 接入 Registry/Verify/通知的测试。"""

import hashlib
import json
import tempfile
import unittest
import uuid

from pathlib import Path

from src.collab.qwen_pipeline import PipelineError, finalize_run, verify_run
from src.collab.registry import ExternalTaskRegistry


class PipelineCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.registry = ExternalTaskRegistry(self.root / "registry.db")
        self.run_id = str(uuid.uuid4())
        self.workspace = self.root / self.run_id / "workspace"
        self.workspace.mkdir(parents=True)
        (self.workspace / "out.txt").write_text("hello", encoding="utf-8")
        self.manifest_path = self.root / self.run_id / "manifest.json"
        self.write_manifest()

    def write_manifest(self, sha256=None, bytes_=None):
        data = (self.workspace / "out.txt").read_bytes()
        self.manifest_path.write_text(
            json.dumps({
                "run_id": self.run_id,
                "artifacts": [{
                    "path": "out.txt",
                    "sha256": sha256 or hashlib.sha256(data).hexdigest(),
                    "bytes": bytes_ if bytes_ is not None else len(data),
                }],
            }),
            encoding="utf-8",
        )
        (self.root / self.run_id / "state.json").write_text(
            json.dumps({
                "run_id": self.run_id,
                "provider_session_id": str(uuid.uuid4()),
                "state": "completed_unverified",
                "observed_model": "qwen3.8-flash",
                "duration_ms": 1000,
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }),
            encoding="utf-8",
        )

    def test_verify_passes(self):
        report = verify_run(self.root, self.run_id)

        self.assertTrue(report.passed)
        self.assertEqual(report.artifacts, ("out.txt",))

    def test_tampered_hash_fails(self):
        self.write_manifest(sha256="0" * 64)

        report = verify_run(self.root, self.run_id)

        self.assertFalse(report.passed)

    def test_missing_file_fails(self):
        (self.workspace / "out.txt").unlink()

        report = verify_run(self.root, self.run_id)

        self.assertFalse(report.passed)

    def test_missing_manifest_raises(self):
        self.manifest_path.unlink()

        with self.assertRaises(PipelineError):
            verify_run(self.root, self.run_id)

    def test_not_ready_rejected(self):
        state_path = self.root / self.run_id / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["state"] = "failed"
        state_path.write_text(json.dumps(state), encoding="utf-8")

        with self.assertRaises(PipelineError) as caught:
            verify_run(self.root, self.run_id)

        self.assertEqual(caught.exception.code, "not_ready")

    def test_finalize_success(self):
        report = finalize_run(root=self.root, run_id=self.run_id, registry=self.registry)

        self.assertEqual(report["state"], "succeeded")
        self.assertEqual(self.registry.get("qwen-cli", self.run_id).state, "succeeded")

    def test_finalize_verify_failure(self):
        self.write_manifest(sha256="f" * 64)

        report = finalize_run(root=self.root, run_id=self.run_id, registry=self.registry)

        self.assertEqual(report["state"], "failed")
        self.assertEqual(self.registry.get("qwen-cli", self.run_id).state, "failed")

    def test_finalize_idempotent_notification(self):
        calls = []

        def sender(text):
            calls.append(text)

        first = finalize_run(root=self.root, run_id=self.run_id, registry=self.registry, sender=sender)
        second = finalize_run(root=self.root, run_id=self.run_id, registry=self.registry, sender=sender)

        self.assertTrue(first["notified"])
        self.assertTrue(second["notified"])
        self.assertEqual(len(calls), 1)

    def test_failed_verify_does_not_notify(self):
        calls = []
        self.write_manifest(bytes_=999)

        finalize_run(root=self.root, run_id=self.run_id, registry=self.registry, sender=lambda text: calls.append(text))

        self.assertEqual(calls, [])

    def test_notification_error_does_not_rollback(self):
        def sender(text):
            raise RuntimeError("qq down")

        report = finalize_run(root=self.root, run_id=self.run_id, registry=self.registry, sender=sender)

        self.assertEqual(report["state"], "succeeded")
        self.assertFalse(report["notified"])
        self.assertIn("qq down", report["notification_error"])



    def test_no_artifacts_plan_passes(self):
        (self.workspace / "out.txt").unlink()
        self.manifest_path.write_text(
            json.dumps({"run_id": self.run_id, "artifacts": []}),
            encoding="utf-8",
        )
        state_path = self.root / self.run_id / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["state"] = "completed_unverified"
        state["approval_mode"] = "plan"
        state_path.write_text(json.dumps(state), encoding="utf-8")

        report = verify_run(self.root, self.run_id)

        self.assertTrue(report.passed)


if __name__ == "__main__":
    unittest.main()
