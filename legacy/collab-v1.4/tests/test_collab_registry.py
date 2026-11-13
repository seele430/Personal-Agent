"""注册表 / 守卫 / 验收测试：幂等、单写者、只认本地结果。"""

import tempfile
import unittest

from pathlib import Path

from src.collab.guard import (
    GuardError,
    WorkspaceBusy,
    WorkspaceGuard,
)
from src.collab.registry import (
    ExternalTaskRegistry,
    RegistryError,
    TaskNotFound,
)
from src.collab.verify import (
    VerifyError,
    check_artifacts,
    check_digest,
    run_command,
    verify,
)


class RegistryCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.registry = ExternalTaskRegistry(Path(self._temporary.name) / "tasks.db")


class TestRegistry(RegistryCase):
    def test_register_and_get(self):
        task = self.registry.register("kimi", "K-1", run_id="r1")

        self.assertEqual(task.state, "submitted")
        self.assertEqual(self.registry.get("kimi", "K-1").run_id, "r1")

    def test_idempotent_by_provider_and_id(self):
        self.registry.register("kimi", "K-1", run_id="r1")
        self.registry.register("kimi", "K-1", run_id="r2", state="running")

        tasks = self.registry.list()

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].run_id, "r2")
        self.assertEqual(tasks[0].state, "running")

    def test_same_id_different_provider_isolated(self):
        self.registry.register("kimi", "K-1", run_id="r1")
        self.registry.register("dsh", "K-1", run_id="r2")

        self.assertEqual(len(self.registry.list()), 2)
        self.assertEqual(self.registry.get("dsh", "K-1").run_id, "r2")

    def test_update_state(self):
        self.registry.register("kimi", "K-1", run_id="r1")

        self.assertTrue(self.registry.update_state("kimi", "K-1", "succeeded"))
        self.assertEqual(self.registry.get("kimi", "K-1").state, "succeeded")

    def test_update_missing_returns_false(self):
        self.assertFalse(self.registry.update_state("kimi", "nope", "succeeded"))

    def test_get_missing_raises(self):
        with self.assertRaises(TaskNotFound):
            self.registry.get("kimi", "nope")

    def test_invalid_state(self):
        with self.assertRaises(RegistryError):
            self.registry.register("kimi", "K-1", run_id="r1", state="done")

    def test_empty_ids_rejected(self):
        with self.assertRaises(RegistryError):
            self.registry.register("", "K-1", run_id="r1")

    def test_invalid_limit(self):
        with self.assertRaises(RegistryError):
            self.registry.list(limit=0)

    def test_claims_are_stored_but_not_evidence(self):
        task = self.registry.register(
            "kimi", "K-1", run_id="r1", claims={"tests_passed": True}
        )

        self.assertEqual(task.claims["tests_passed"], True)
        # 自述只存档：验收函数根本不读 claims
        result = verify(
            artifact_dir=self._temporary.name, expected=("nope.txt",)
        )
        self.assertFalse(result.passed)

    def test_artifacts_listed(self):
        task = self.registry.register(
            "kimi", "K-1", run_id="r1", artifacts=("a.md", "b.png")
        )

        self.assertEqual(task.artifacts, ("a.md", "b.png"))

    def test_list_filters_provider(self):
        self.registry.register("kimi", "K-1", run_id="r1")
        self.registry.register("dsh", "D-1", run_id="r2", state="running")

        self.assertEqual(len(self.registry.list(provider="dsh")), 1)


class GuardCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        (self.workspace / "a.txt").write_text("hello", encoding="utf-8")
        self.guard = WorkspaceGuard(self.root / "guard.db", self.workspace)


class TestGuard(GuardCase):
    def test_single_writer(self):
        self.guard.acquire("agent-a")

        with self.assertRaises(WorkspaceBusy):
            self.guard.acquire("agent-b")

    def test_same_owner_reacquire(self):
        self.guard.acquire("agent-a")
        lease = self.guard.acquire("agent-a")

        self.assertEqual(lease.owner, "agent-a")

    def test_release_allows_others(self):
        self.guard.acquire("agent-a")
        self.assertTrue(self.guard.release("agent-a"))

        self.guard.acquire("agent-b")

    def test_release_by_wrong_owner_does_nothing(self):
        self.guard.acquire("agent-a")

        self.assertFalse(self.guard.release("agent-b"))

    def test_expired_lease_can_be_taken(self):
        self.guard.acquire("agent-a", ttl_seconds=1)

        import time

        time.sleep(1.1)

        self.assertEqual(self.guard.acquire("agent-b").owner, "agent-b")

    def test_invalid_ttl(self):
        with self.assertRaises(GuardError):
            self.guard.acquire("agent-a", ttl_seconds=0)

    def test_snapshot_hashes(self):
        snapshot = self.guard.snapshot()

        self.assertIn("a.txt", snapshot)
        self.assertEqual(len(snapshot["a.txt"]), 16)

    def test_diff_detects_changes(self):
        before = self.guard.snapshot()
        (self.workspace / "a.txt").write_text("changed", encoding="utf-8")
        (self.workspace / "b.txt").write_text("new", encoding="utf-8")
        after = self.guard.snapshot()

        diff = self.guard.diff(before, after)

        self.assertTrue(diff.dirty)
        self.assertIn("b.txt", diff.added)
        self.assertIn("a.txt", diff.changed)

    def test_diff_detects_removed(self):
        before = self.guard.snapshot()
        (self.workspace / "a.txt").unlink()

        diff = self.guard.diff(before, self.guard.snapshot())

        self.assertIn("a.txt", diff.removed)

    def test_clean_diff(self):
        diff = self.guard.diff(self.guard.snapshot(), self.guard.snapshot())

        self.assertFalse(diff.dirty)
        self.assertEqual(diff.to_dict()["added"], [])


class TestVerify(GuardCase):
    def test_artifacts_present(self):
        check = check_artifacts(self.workspace, ("a.txt",))

        self.assertTrue(check.ok)

    def test_artifacts_missing(self):
        check = check_artifacts(self.workspace, ("nope.txt",))

        self.assertFalse(check.ok)
        self.assertIn("nope.txt", check.detail)

    def test_digest_matches(self):
        import hashlib

        digest = hashlib.sha256(b"hello").hexdigest()

        self.assertTrue(check_digest(self.workspace / "a.txt", digest).ok)

    def test_digest_mismatch(self):
        self.assertFalse(check_digest(self.workspace / "a.txt", "0" * 64).ok)

    def test_command_exit_zero(self):
        check = run_command(
            ["cmd", "/c", "echo", "ok"], cwd=self.workspace
        )

        self.assertTrue(check.ok, check.detail)

    def test_command_failure(self):
        check = run_command(
            ["cmd", "/c", "exit", "1"], cwd=self.workspace
        )

        self.assertFalse(check.ok)

    def test_verify_combines(self):
        result = verify(artifact_dir=self.workspace, expected=("a.txt",))

        self.assertTrue(result.passed)
        self.assertEqual(len(result.checks), 1)

    def test_verify_requires_a_check(self):
        with self.assertRaises(VerifyError):
            verify(artifact_dir=self.workspace)

    def test_verify_serializes(self):
        payload = verify(artifact_dir=self.workspace, expected=("a.txt",)).to_dict()

        self.assertTrue(payload["passed"])
        self.assertEqual(payload["checks"][0]["name"], "artifacts")

    def test_failed_command_fails_verify(self):
        result = verify(
            artifact_dir=self.workspace,
            expected=("a.txt",),
            command=["cmd", "/c", "exit", "2"],
        )

        self.assertFalse(result.passed)


if __name__ == "__main__":
    unittest.main()
