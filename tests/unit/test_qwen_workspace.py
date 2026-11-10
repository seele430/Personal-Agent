"""工作区与产物扫描测试。"""

import json
import os
import tempfile
import unittest

from pathlib import Path

from src.collab.qwen_workspace import ArtifactError, RunWorkspace


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.ws = RunWorkspace(self.root, "run-1").create()

    def test_manifest_hashes(self):
        (self.ws.workspace / "a.txt").write_text("hello", encoding="utf-8")

        manifest = self.ws.scan()

        self.assertEqual(len(manifest), 1)
        self.assertEqual(manifest[0]["path"], "a.txt")
        self.assertEqual(manifest[0]["bytes"], 5)
        self.assertEqual(len(manifest[0]["sha256"]), 64)

    def test_state_and_manifest_not_in_artifacts(self):
        (self.ws.workspace / "a.txt").write_text("x", encoding="utf-8")
        self.ws.write_state({"state": "running"})
        self.ws.scan()

        paths = [item["path"] for item in json.loads(self.ws.manifest_path.read_text(encoding="utf-8"))["artifacts"]]

        self.assertEqual(paths, ["a.txt"])

    def test_reject_traversal(self):
        outside = self.root / "secret.txt"
        outside.write_text("secret", encoding="utf-8")
        link = self.ws.workspace / "link.txt"

        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink 不可用")

        with self.assertRaises(ArtifactError):
            self.ws.scan()

    def test_expected_missing(self):
        with self.assertRaises(ArtifactError) as caught:
            self.ws.scan({"missing.txt": False})

        self.assertEqual(caught.exception.code, "artifact_missing")

    def test_expected_empty_rejected(self):
        (self.ws.workspace / "empty.txt").write_text("", encoding="utf-8")

        with self.assertRaises(ArtifactError) as caught:
            self.ws.scan({"empty.txt": False})

        self.assertEqual(caught.exception.code, "artifact_missing")

    def test_expected_empty_allowed(self):
        (self.ws.workspace / "empty.txt").write_text("", encoding="utf-8")

        manifest = self.ws.scan({"empty.txt": True})

        self.assertEqual(manifest[0]["path"], "empty.txt")

    def test_too_many_files(self):
        for index in range(11):
            (self.ws.workspace / f"f{index}.txt").write_text("x", encoding="utf-8")

        with self.assertRaises(ArtifactError) as caught:
            self.ws.scan()

        self.assertEqual(caught.exception.code, "artifact_rejected")

    def test_atomic_state_write(self):
        self.ws.write_state({"state": "completed_unverified"})

        self.assertEqual(self.ws.read_state()["state"], "completed_unverified")
        self.assertFalse(self.ws.state_path.with_suffix(".json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
