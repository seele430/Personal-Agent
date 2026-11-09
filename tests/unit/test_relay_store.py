"""接力存储测试：认领、租约、fencing、resume/continue、步骤。"""

import tempfile
import unittest

from pathlib import Path

from src.collab.run_store import RunStore, StoreError


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.now = 1000.0
        self.store = RunStore(Path(self._temporary.name) / "runs.db", clock=lambda: self.now)
        self.run_id = self.store.create_run("写一篇文章", ["ark", "deepseek"])

    def claim(self, owner="w1"):
        return self.store.claim(self.run_id, owner)

    def test_create_and_claim(self):
        run = self.claim()

        self.assertEqual(run["state"], "running")
        self.assertEqual(run["fencing_token"], 1)
        self.assertEqual(run["chain"], ["ark", "deepseek"])

    def test_second_claim_rejected_while_lease_active(self):
        self.claim()
        self.assertIsNone(self.claim("w2"))

    def test_expired_lease_can_be_claimed(self):
        self.claim()
        self.now += 1000

        run = self.claim("w2")

        self.assertEqual(run["fencing_token"], 2)

    def test_old_worker_cannot_heartbeat(self):
        self.claim()
        self.now += 1000
        self.claim("w2")

        self.assertFalse(self.store.heartbeat(self.run_id, 1))
        self.assertTrue(self.store.heartbeat(self.run_id, 2))

    def test_old_worker_cannot_finish(self):
        self.claim()
        self.now += 1000
        self.claim("w2")

        self.assertFalse(self.store.finish(self.run_id, 1, "succeeded"))
        self.assertEqual(self.store.get(self.run_id)["state"], "running")

    def test_resume_failed_run(self):
        run = self.claim()
        self.store.finish(self.run_id, run["fencing_token"], "failed", error_class="retryable")

        resumed = self.store.resume(self.run_id)

        self.assertEqual(resumed["state"], "pending")

    def test_resume_succeeded_rejected(self):
        run = self.claim()
        self.store.finish(self.run_id, run["fencing_token"], "succeeded")

        with self.assertRaises(StoreError) as caught:
            self.store.resume(self.run_id)

        self.assertIn("already_succeeded", str(caught.exception))

    def test_resume_needs_review_rejected(self):
        run = self.claim()
        self.store.mark_needs_review(self.run_id, run["fencing_token"], "step review")

        with self.assertRaises(StoreError) as caught:
            self.store.resume(self.run_id)

        self.assertIn("needs_review", str(caught.exception))

    def test_continue_bumps_revision_and_keeps_messages(self):
        run = self.claim()
        self.store.finish(self.run_id, run["fencing_token"], "succeeded")

        continued = self.store.continue_run(self.run_id, "再补一章")

        self.assertEqual(continued["revision"], 2)
        self.assertEqual(continued["state"], "pending")
        self.assertIn("再补一章", continued["messages_json"])

    def test_continue_requires_succeeded(self):
        with self.assertRaises(StoreError):
            self.store.continue_run(self.run_id, "x")

    def test_step_done_then_skip(self):
        run = self.claim()
        token = run["fencing_token"]

        self.assertEqual(self.store.step_begin(self.run_id, 1, "compose", "task", token), "execute")
        self.assertTrue(self.store.step_done(self.run_id, 1, "compose", token, {"path": "a"}))

        self.assertEqual(self.store.step_begin(self.run_id, 1, "compose", "task", token), "skip")

    def test_running_step_becomes_needs_review(self):
        run = self.claim()

        self.store.step_begin(self.run_id, 1, "compose", "task", run["fencing_token"])

        self.assertEqual(
            self.store.step_begin(self.run_id, 1, "compose", "task", run["fencing_token"]),
            "needs_review",
        )

    def test_accept_and_retry_step(self):
        run = self.claim()
        token = run["fencing_token"]
        self.store.step_begin(self.run_id, 1, "compose", "task", token)
        self.store.step_begin(self.run_id, 1, "compose", "task", token)

        self.assertTrue(self.store.step_accept(self.run_id, 1, "compose", token, {"path": "a", "sha256": "x", "bytes": 1}))
        self.assertEqual(self.store.step_state(self.run_id, 1, "compose"), "done")

    def test_attempts_recorded(self):
        run = self.claim()
        attempt_id = self.store.attempt_start(self.run_id, 1, 1, "ark", run["fencing_token"])
        self.store.attempt_finish(attempt_id, "failed", token=run["fencing_token"], error_class="retryable", error="timeout")

        attempts = self.store.attempts(self.run_id)

        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["error_class"], "retryable")



    def test_commit_rejects_stale_fencing(self):
        run = self.claim()
        self.store.step_begin(self.run_id, 1, "compose", "task", run["fencing_token"])
        staging = Path(self._temporary.name) / "staging.md"
        staging.write_bytes(b"hello")
        target = Path(self._temporary.name) / "out.md"

        self.now += 1000
        self.claim("w2")

        ok = self.store.commit_step_artifact(
            self.run_id, 1, "compose", 1,
            staging_path=str(staging), target_path=str(target),
            manifest={"path": "out.md", "sha256": __import__("hashlib").sha256(b"hello").hexdigest(), "bytes": 5},
        )

        self.assertFalse(ok)
        self.assertFalse(target.exists())

    def test_commit_moves_file_and_writes_manifest(self):
        run = self.claim()
        self.store.step_begin(self.run_id, 1, "compose", "task", run["fencing_token"])
        staging = Path(self._temporary.name) / "staging.md"
        staging.write_bytes(b"hello")
        target = Path(self._temporary.name) / "out.md"

        ok = self.store.commit_step_artifact(
            self.run_id, 1, "compose", run["fencing_token"],
            staging_path=str(staging), target_path=str(target),
            manifest={"path": "out.md", "sha256": __import__("hashlib").sha256(b"hello").hexdigest(), "bytes": 5},
        )

        self.assertTrue(ok)
        self.assertEqual(target.read_bytes(), b"hello")
        self.assertEqual(self.store.step_state(self.run_id, 1, "compose"), "done")

    def test_cleanup_removes_orphans_and_keeps_referenced(self):
        root = Path(self._temporary.name) / "artifacts"
        run = self.claim()
        self.store.step_begin(self.run_id, 1, "compose", "task", run["fencing_token"])
        referenced = root / "keep.md"
        referenced.parent.mkdir(parents=True, exist_ok=True)
        staging = root / ".staging" / "keep-staging.md"
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(b"keep")
        orphan = root / "orphan.md"
        orphan.write_bytes(b"orphan")

        self.store.commit_step_artifact(
            self.run_id, 1, "compose", run["fencing_token"],
            staging_path=str(staging), target_path=str(referenced),
            manifest={"path": "keep.md", "sha256": __import__("hashlib").sha256(b"keep").hexdigest(), "bytes": 4},
        )

        result = self.store.cleanup_orphans(root, orphan_min_age=0)

        self.assertTrue(referenced.exists())
        self.assertFalse(orphan.exists())
        self.assertIn("orphan.md", result["removed"])



    def test_commit_aborts_when_replace_hook_fails(self):
        run = self.claim()
        self.store.step_begin(self.run_id, 1, "compose", "task", run["fencing_token"])
        staging = Path(self._temporary.name) / "s.md"
        staging.write_bytes(b"hello")
        target = Path(self._temporary.name) / "t.md"

        def boom():
            raise RuntimeError("injected")

        ok = self.store.commit_step_artifact(
            self.run_id, 1, "compose", run["fencing_token"],
            staging_path=str(staging), target_path=str(target),
            manifest={"path": "t.md", "sha256": "x", "bytes": 5},
            before_replace=boom,
        )

        self.assertFalse(ok)
        self.assertFalse(target.exists())
        self.assertTrue(staging.exists())

    def test_cleanup_removes_old_staging(self):
        import os as _os
        import time as _time

        root = Path(self._temporary.name) / "artifacts"
        staging = root / ".staging" / "old"
        staging.mkdir(parents=True)
        old = _time.time() - 7200
        _os.utime(staging, (old, old))

        result = self.store.cleanup_orphans(root, orphan_min_age=0)

        self.assertFalse(staging.exists())
        self.assertIn(".staging/old", result["removed"])



    def test_old_token_all_writes_rejected(self):
        from src.collab.run_store import StaleWorker

        run = self.claim()
        old = run["fencing_token"]
        self.store.step_begin(self.run_id, 1, "compose", "task", old)

        self.now += 1000
        new_run = self.claim("w2")
        new = new_run["fencing_token"]

        with self.assertRaises(StaleWorker):
            self.store.attempt_start(self.run_id, 1, 9, "x", old)

        self.assertEqual(self.store.step_begin(self.run_id, 1, "compose", "task", old), "stale")

        self.assertFalse(
            self.store.finish(self.run_id, old, "succeeded")
        )
        self.assertFalse(self.store.heartbeat(self.run_id, old))
        self.assertFalse(self.store.step_accept(self.run_id, 1, "compose", old, {}))
        self.assertFalse(self.store.step_retry(self.run_id, 1, "compose", old))
        self.assertFalse(self.store.set_pending_after_review(self.run_id, old))

        attempt_id = self.store.attempt_start(self.run_id, 1, 10, "x", new)
        self.assertFalse(self.store.attempt_finish(attempt_id, "succeeded", token=old))
        self.assertTrue(self.store.attempt_finish(attempt_id, "succeeded", token=new))

    def test_commit_failure_restores_existing_target(self):
        run = self.claim()
        self.store.step_begin(self.run_id, 1, "compose", "task", run["fencing_token"])
        target = Path(self._temporary.name) / "target.md"
        target.write_bytes(b"known-good")
        staging = Path(self._temporary.name) / "staging.md"
        staging.write_bytes(b"new-data")

        ok = self.store.commit_step_artifact(
            self.run_id, 1, "compose", run["fencing_token"],
            staging_path=str(staging), target_path=str(target),
            manifest={"path": "target.md", "sha256": "wrong", "bytes": 8},
        )

        self.assertFalse(ok)
        self.assertEqual(target.read_bytes(), b"known-good")

    def test_resume_preserves_provider_index(self):
        run = self.claim()
        self.store.set_next_provider(self.run_id, run["fencing_token"], 1, "b")
        self.store.finish(self.run_id, run["fencing_token"], "failed", error_class="retryable")

        self.store.resume(self.run_id)

        self.assertEqual(self.store.get(self.run_id)["next_provider_index"], 1)

        self.store.reset_chain(self.run_id)
        self.assertEqual(self.store.get(self.run_id)["next_provider_index"], 0)


if __name__ == "__main__":
    unittest.main()
