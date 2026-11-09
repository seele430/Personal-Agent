"""CLI accept/retry 端到端：manifest 复核、调度、审计。"""

import os
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

import scripts.collab_relay as cli
from src.collab import artifacts
from src.collab.run_store import RunStore


class FakeProvider:
    injected = False

    def __init__(self, text="ok", fail=False):
        self.text = text
        self.fail = fail
        self.calls = 0

    def submit(self, prompt, *, artifact_name, out_dir):
        self.calls += 1

        if self.fail:
            raise RuntimeError("boom")

        path = Path(out_dir) / artifact_name
        path.write_text(self.text, encoding="utf-8")

        return {"artifact": str(path)}


class CliCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.db = self.root / "runs.db"
        self.artifacts_root = self.root / "artifacts"
        self.env = patch.dict(
            os.environ,
            {"PA_COLLAB_DB": str(self.db), "PA_COLLAB_ARTIFACTS": str(self.artifacts_root)},
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = RunStore(self.db)
        self.run_id = self.store.create_run("写一篇文章", ["a"])

    def to_review(self, manifest=None):
        claimed = self.store.claim(self.run_id, "cli")
        token = claimed["fencing_token"]
        self.store.step_begin(self.run_id, 1, "compose", "写一篇文章", token)
        self.store.step_begin(self.run_id, 1, "compose", "写一篇文章", token)
        self.store.mark_needs_review(self.run_id, token, "test")

        if manifest is not None:
            import json

            with self.store._session() as connection:
                connection.execute(
                    "UPDATE collab_steps SET artifact_manifest=? WHERE run_id=? AND revision=1 AND step_id='compose'",
                    (json.dumps(manifest, ensure_ascii=False), self.run_id),
                )

    def test_resume_needs_review_exits_2(self):
        self.to_review()

        self.assertEqual(cli.main(["resume", "--run-id", self.run_id]), 2)

    def test_accept_without_manifest_exits_2(self):
        self.to_review()

        self.assertEqual(cli.main(["resume", "--run-id", self.run_id, "--accept-step", "compose"]), 2)

    def test_accept_with_valid_manifest_succeeds_without_provider(self):
        manifest = artifacts.commit(self.artifacts_root, f"{self.run_id}/r1/result.md", b"hello")
        self.to_review(manifest)
        provider = FakeProvider(fail=True)

        with patch.object(cli, "build_providers", return_value={"a": provider}):
            code = cli.main(["resume", "--run-id", self.run_id, "--accept-step", "compose"])

        self.assertEqual(code, 0)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(self.store.get(self.run_id)["state"], "succeeded")

    def test_accept_with_tampered_manifest_exits_2(self):
        manifest = artifacts.commit(self.artifacts_root, f"{self.run_id}/r1/result.md", b"hello")
        self.to_review(manifest)
        (self.artifacts_root / f"{self.run_id}/r1/result.md").write_bytes(b"tampered")

        self.assertEqual(cli.main(["resume", "--run-id", self.run_id, "--accept-step", "compose"]), 2)

    def test_retry_runs_provider_once(self):
        self.to_review()
        provider = FakeProvider()

        with patch.object(cli, "build_providers", return_value={"a": provider}):
            code = cli.main(["resume", "--run-id", self.run_id, "--retry-step", "compose"])

        self.assertEqual(code, 0)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(self.store.get(self.run_id)["state"], "succeeded")



    def test_resume_succeeded_exits_2(self):
        claimed = self.store.claim(self.run_id, "cli")
        self.store.finish(self.run_id, claimed["fencing_token"], "succeeded")

        self.assertEqual(cli.main(["resume", "--run-id", self.run_id]), 2)

    def test_retry_unknown_step_exits_2(self):
        self.to_review()

        self.assertEqual(cli.main(["resume", "--run-id", self.run_id, "--retry-step", "nope"]), 2)



    def test_accept_without_path_rejected(self):
        self.to_review({"sha256": "x", "bytes": 1})

        self.assertEqual(cli.main(["resume", "--run-id", self.run_id, "--accept-step", "compose"]), 2)

    def test_cli_startup_cleanup_removes_orphan(self):
        import os as _os
        import time as _time

        orphan = self.artifacts_root / "orphan.md"
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(b"orphan")
        old = _time.time() - 7200
        _os.utime(orphan, (old, old))

        with patch.object(cli, "build_providers", return_value={"a": FakeProvider()}):
            code = cli.main(["run", "--task", "x", "--chain", "a"])

        self.assertEqual(code, 0)
        self.assertFalse(orphan.exists())


if __name__ == "__main__":
    unittest.main()
