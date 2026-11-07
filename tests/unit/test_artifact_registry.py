"""产物登记与路径安全测试。

路径安全是这一层的核心：`../`、绝对路径、符号链接、同级前缀目录都必须被拒。
"""

import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from src.artifacts.models import (
    ArtifactError,
    ArtifactForbidden,
    ArtifactNotFound,
    classify,
)
from src.artifacts.registry import ArtifactRegistry


class RegistryTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        self.base = Path(self.temp.name)
        self.root = self.base / "artifacts"
        self.root.mkdir()

        self.registry = ArtifactRegistry(
            self.base / "artifacts.db", root=self.root
        )

    def write(self, name, content=b"data", *, folder=None):
        target = Path(folder or self.root) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

        return target


class TestRegistration(RegistryTestCase):
    def test_register_image(self):
        path = self.write("shot.png", b"\x89PNG\r\n")

        artifact = self.registry.register("u1", path)

        self.assertTrue(artifact.id.startswith("art_"))
        self.assertEqual(artifact.kind, "image")
        self.assertEqual(artifact.media_type, "image/png")
        self.assertEqual(artifact.size_bytes, 6)
        self.assertEqual(len(artifact.sha256), 64)
        self.assertEqual(artifact.title, "shot.png")
        self.assertEqual(artifact.rel_path, "shot.png")

    def test_register_markdown_and_text(self):
        markdown = self.registry.register("u1", self.write("report.md", b"# hi"))
        text = self.registry.register("u1", self.write("run.log", b"log"))

        self.assertEqual(markdown.kind, "markdown")
        self.assertEqual(text.kind, "text")

    def test_register_pdf_and_other(self):
        pdf = self.registry.register("u1", self.write("paper.pdf", b"%PDF-1.7"))
        other = self.registry.register("u1", self.write("blob.bin", b"\x00\x01"))

        self.assertEqual(pdf.kind, "pdf")
        self.assertEqual(other.kind, "other")

    def test_relative_path_is_resolved_against_root(self):
        self.write("nested/deep.txt", b"x")

        artifact = self.registry.register("u1", "nested/deep.txt")

        self.assertEqual(artifact.rel_path, "nested/deep.txt")

    def test_title_override_and_length_limit(self):
        path = self.write("a.txt", b"x")

        artifact = self.registry.register("u1", path, title="  自定义标题  ")
        self.assertEqual(artifact.title, "自定义标题")

        with self.assertRaises(ArtifactError):
            self.registry.register("u1", path, title="长" * 201)

    def test_duplicate_registration_shares_sha(self):
        path = self.write("same.txt", b"content")

        first = self.registry.register("u1", path)
        second = self.registry.register("u1", path, title="副本")

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(first.sha256, second.sha256)

    def test_oversized_file_is_rejected(self):
        path = self.write("big.txt", b"x" * 100)

        with patch("src.artifacts.registry.MAX_ARTIFACT_BYTES", 10):
            with self.assertRaises(ArtifactError):
                self.registry.register("u1", path)


class TestPathSafety(RegistryTestCase):
    def test_outside_absolute_path_is_rejected(self):
        outside = self.write("outside.txt", b"x", folder=self.base)

        with self.assertRaises(ArtifactForbidden):
            self.registry.register("u1", outside)

    def test_traversal_is_rejected(self):
        self.write("outside.txt", b"x", folder=self.base)

        with self.assertRaises(ArtifactForbidden):
            self.registry.register("u1", "../outside.txt")

    def test_sibling_prefix_directory_is_rejected(self):
        evil = self.base / "artifacts-evil"
        evil.mkdir()
        (evil / "x.txt").write_bytes(b"x")

        with self.assertRaises(ArtifactForbidden):
            self.registry.register("u1", evil / "x.txt")

    def test_directory_is_rejected(self):
        folder = self.root / "sub"
        folder.mkdir()

        with self.assertRaises(ArtifactError):
            self.registry.register("u1", folder)

    def test_missing_file_is_rejected(self):
        with self.assertRaises(ArtifactNotFound):
            self.registry.register("u1", "nope.txt")

    def test_symlink_escape_is_rejected(self):
        outside = self.write("secret.txt", b"x", folder=self.base)
        link = self.root / "link.txt"

        try:
            link.symlink_to(outside)

        except (OSError, NotImplementedError):
            self.skipTest("本机不允许创建符号链接")

        with self.assertRaises(ArtifactForbidden):
            self.registry.register("u1", link)


class TestReadAndDelete(RegistryTestCase):
    def test_owner_isolation(self):
        artifact = self.registry.register("u1", self.write("a.txt", b"x"))

        with self.assertRaises(ArtifactForbidden):
            self.registry.get(artifact.id, owner_id="u2")

        self.assertEqual(self.registry.get(artifact.id, owner_id="u1").id, artifact.id)

    def test_unknown_id(self):
        with self.assertRaises(ArtifactNotFound):
            self.registry.get("art_nope", owner_id="u1")

    def test_list_filters_by_run_and_owner(self):
        self.registry.register("u1", self.write("a.txt", b"a"), run_id="run-1")
        self.registry.register("u1", self.write("b.txt", b"b"), run_id="run-2")
        self.registry.register("u2", self.write("c.txt", b"c"), run_id="run-1")

        self.assertEqual(len(self.registry.list("u1")), 2)
        self.assertEqual(len(self.registry.list("u1", run_id="run-1")), 1)

        with self.assertRaises(ValueError):
            self.registry.list("u1", limit=0)

    def test_absolute_path_and_missing_file(self):
        path = self.write("gone.txt", b"x")
        artifact = self.registry.register("u1", path)

        self.assertEqual(
            self.registry.absolute_path(artifact.id, owner_id="u1").resolve(),
            path.resolve(),
        )

        path.unlink()

        with self.assertRaises(ArtifactNotFound):
            self.registry.absolute_path(artifact.id, owner_id="u1")

    def test_delete_removes_row_and_optional_file(self):
        path = self.write("temp.txt", b"x")
        artifact = self.registry.register("u1", path)

        self.assertTrue(
            self.registry.delete(artifact.id, owner_id="u1", purge_file=True)
        )
        self.assertEqual(self.registry.count("u1"), 0)
        self.assertFalse(path.exists())

    def test_delete_without_purge_keeps_file(self):
        path = self.write("keep.txt", b"x")
        artifact = self.registry.register("u1", path)

        self.registry.delete(artifact.id, owner_id="u1")

        self.assertTrue(path.exists())
        self.assertEqual(self.registry.count(), 0)


class TestClassify(unittest.TestCase):
    def test_media_type_wins_over_suffix(self):
        self.assertEqual(classify("image/png", "weird.bin"), "image")
        self.assertEqual(classify("application/pdf", "x.txt"), "pdf")
        self.assertEqual(classify("text/markdown", "x.txt"), "markdown")

    def test_suffix_fallback(self):
        self.assertEqual(classify("", "a.PNG"), "image")
        self.assertEqual(classify("", "a.md"), "markdown")
        self.assertEqual(classify("application/octet-stream", "a.log"), "text")
        self.assertEqual(classify("", "a.exe"), "other")


if __name__ == "__main__":
    unittest.main()
