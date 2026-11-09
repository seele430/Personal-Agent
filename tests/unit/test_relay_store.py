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

        self.assertTrue(self.store.step_accept(self.run_id, 1, "compose", token, {"path": "a"}))
        self.assertEqual(self.store.step_state(self.run_id, 1, "compose"), "done")

    def test_attempts_recorded(self):
        run = self.claim()
        attempt_id = self.store.attempt_start(self.run_id, 1, 1, "ark", run["fencing_token"])
        self.store.attempt_finish(attempt_id, "failed", error_class="retryable", error="timeout")

        attempts = self.store.attempts(self.run_id)

        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["error_class"], "retryable")


if __name__ == "__main__":
    unittest.main()
