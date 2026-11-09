"""产物原子落盘测试：边界、符号链接、哈希。"""

import os
import tempfile
import unittest

from pathlib import Path

from src.collab.artifacts import ArtifactError, commit, verify


class ArtifactCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name) / "out"
        self.root.mkdir()

    def test_commit_and_verify(self):
        manifest = commit(self.root, "r1/result.md", b"hello")

        self.assertEqual(manifest["bytes"], 5)
        self.assertTrue(verify(self.root, manifest))
        self.assertEqual((self.root / "r1" / "result.md").read_bytes(), b"hello")

    def test_absolute_path_rejected(self):
        with self.assertRaises(ArtifactError):
            commit(self.root, "C:/Windows/x.md", b"x")

    def test_parent_traversal_rejected(self):
        with self.assertRaises(ArtifactError):
            commit(self.root, "../x.md", b"x")

    def test_symlink_escape_rejected(self):
        link = self.root / "link"
        outside = Path(self._temporary.name) / "outside"
        outside.mkdir()

        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink 不可用")

        with self.assertRaises(ArtifactError):
            commit(self.root, "link/x.md", b"x")

    def test_verify_mismatch(self):
        manifest = commit(self.root, "a.md", b"hello")
        (self.root / "a.md").write_bytes(b"tampered")

        self.assertFalse(verify(self.root, manifest))

    def test_empty_path_rejected(self):
        with self.assertRaises(ArtifactError):
            commit(self.root, "", b"x")



    def test_verify_rejects_absolute_path(self):
        outside = Path(self._temporary.name) / "secret.md"
        outside.write_bytes(b"secret")
        manifest = {"path": str(outside), "sha256": __import__("hashlib").sha256(b"secret").hexdigest(), "bytes": 6}

        self.assertFalse(verify(self.root, manifest))

    def test_verify_rejects_parent_traversal(self):
        outside = Path(self._temporary.name) / "secret.md"
        outside.write_bytes(b"secret")
        manifest = {"path": "../secret.md", "sha256": __import__("hashlib").sha256(b"secret").hexdigest(), "bytes": 6}

        self.assertFalse(verify(self.root, manifest))

    def test_verify_rejects_symlink_escape(self):
        outside = Path(self._temporary.name) / "secret.md"
        outside.write_bytes(b"secret")
        link = self.root / "link.md"

        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink 不可用")

        manifest = {"path": "link.md", "sha256": __import__("hashlib").sha256(b"secret").hexdigest(), "bytes": 6}
        self.assertFalse(verify(self.root, manifest))


if __name__ == "__main__":
    unittest.main()
