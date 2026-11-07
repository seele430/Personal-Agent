"""Diff 核心测试：文本/二进制/截断/编码/只读。"""

import hashlib
import tempfile
import unittest

from pathlib import Path

from src.diff.compare import (
    DiffError,
    compare_files,
    compare_text,
    decode_text,
    read_bytes,
)


class CompareCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def write(self, name, data: bytes):
        path = self.root / name
        path.write_bytes(data)

        return path


class TestText(unittest.TestCase):
    def test_identical(self):
        result = compare_text("a", "b", "x\ny", "x\ny")

        self.assertEqual(result.kind, "identical")
        self.assertFalse(result.changed)

    def test_added_line(self):
        result = compare_text("a", "b", "x", "x\ny")

        self.assertEqual(result.added, 1)
        self.assertEqual(result.removed, 0)
        self.assertTrue(any(line.kind == "add" for line in result.lines))

    def test_removed_line(self):
        result = compare_text("a", "b", "x\ny", "x")

        self.assertEqual(result.removed, 1)

    def test_changed_line_counts_both(self):
        result = compare_text("a", "b", "one\ntwo", "one\nTWO")

        self.assertEqual((result.added, result.removed), (1, 1))

    def test_negative_context_rejected(self):
        with self.assertRaises(DiffError):
            compare_text("a", "b", "x", "y", context=-1)

    def test_serializes(self):
        payload = compare_text("a", "b", "x", "x\ny").to_dict()

        self.assertIn("lines", payload)
        self.assertTrue(payload["changed"])


class TestDecode(unittest.TestCase):
    def test_utf8(self):
        self.assertEqual(decode_text("中文".encode("utf-8")), "中文")

    def test_gbk_fallback(self):
        self.assertEqual(decode_text("中文".encode("gbk")), "中文")

    def test_binary_returns_none(self):
        self.assertIsNone(decode_text(b"\x00\x01\x02"))

    def test_invalid_bytes_returns_none(self):
        self.assertIsNone(decode_text(bytes(range(128, 256))))


class TestFiles(CompareCase):
    def test_text_files(self):
        a = self.write("a.md", "第一行\n".encode("utf-8"))
        b = self.write("b.md", "第一行\n第二行\n".encode("utf-8"))

        result = compare_files(a, b)

        self.assertEqual(result.kind, "text")
        self.assertEqual(result.added, 1)
        self.assertEqual(result.a_size, a.stat().st_size)

    def test_binary_files(self):
        a = self.write("a.bin", b"\x00\x01")
        b = self.write("b.bin", b"\x00\x02")

        result = compare_files(a, b)

        self.assertEqual(result.kind, "binary")
        self.assertTrue(result.changed)
        self.assertNotEqual(result.a_digest, result.b_digest)

    def test_identical_binary(self):
        a = self.write("a.bin", b"\x00\x01")
        b = self.write("b.bin", b"\x00\x01")

        self.assertEqual(compare_files(a, b).kind, "identical")

    def test_truncation_flag(self):
        a = self.write("a.txt", b"x" * 100)
        b = self.write("b.txt", b"x" * 100 + b"y")

        result = compare_files(a, b, max_bytes=10)

        self.assertTrue(result.truncated)

    def test_missing_file(self):
        with self.assertRaises(DiffError):
            compare_files(self.root / "nope", self.write("b.txt", b"x"))

    def test_read_only(self):
        a = self.write("a.txt", b"abc")
        before = hashlib.sha256(a.read_bytes()).hexdigest()

        compare_files(a, self.write("b.txt", b"abd"))

        self.assertEqual(hashlib.sha256(a.read_bytes()).hexdigest(), before)

    def test_invalid_max_bytes(self):
        a = self.write("a.txt", b"x")

        with self.assertRaises(DiffError):
            read_bytes(a, max_bytes=0)


if __name__ == "__main__":
    unittest.main()
