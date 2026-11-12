"""策略测试：路径边界、确认定位符、输出截断。"""

import os
import tempfile
import unittest

from pathlib import Path

from src.qq.policy import (
    CONFIRM_ALPHABET,
    PolicyError,
    chunk_for_qq,
    decide,
    generate_confirmation_id,
    params_hash,
    truncate_for_qq,
    validate_read_path,
)


class ConfirmationCase(unittest.TestCase):
    def test_id_format(self):
        identifier = generate_confirmation_id()

        self.assertEqual(len(identifier), 8)
        self.assertTrue(all(char in CONFIRM_ALPHABET for char in identifier))


class ChunkCase(unittest.TestCase):
    def test_short_text_single_chunk(self):
        self.assertEqual(chunk_for_qq("短回答", 800, 3), ["短回答"])

    def test_empty_text_no_chunk(self):
        self.assertEqual(chunk_for_qq("   ", 800, 3), [])

    def test_long_text_splits_without_loss(self):
        text = "\n\n".join(f"第{i}段。" + "内容" * 60 for i in range(6))
        chunks = chunk_for_qq(text, 300, 3)

        self.assertGreater(len(chunks), 1)
        self.assertLessEqual(len(chunks), 3)
        self.assertEqual("".join(chunks).replace("\n", ""), text.replace("\n", ""))
        for chunk in chunks[:-1]:
            self.assertLessEqual(len(chunk), 300)

    def test_overflow_merges_into_last_chunk(self):
        text = "\n\n".join("段落" * 40 for _ in range(12))
        chunks = chunk_for_qq(text, 200, 3)

        self.assertEqual(len(chunks), 3)
        self.assertEqual("".join(chunks).replace("\n", ""), text.replace("\n", ""))

    def test_cuts_on_sentence_end(self):
        text = "第一句。" * 100
        chunks = chunk_for_qq(text, 200, 3)

        self.assertTrue(all(chunk.endswith("。") for chunk in chunks[:-1]))

    def test_very_long_text_balances_chunks(self):
        text = "长" * 12700
        chunks = chunk_for_qq(text, 800, 3)

        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(len(chunk) <= 4500 for chunk in chunks))
        self.assertEqual("".join(chunks), text)

    def test_ids_are_unique_enough(self):
        ids = {generate_confirmation_id() for _ in range(200)}

        self.assertGreaterEqual(len(ids), 195)

    def test_params_hash_is_stable(self):
        self.assertEqual(params_hash({"a": 1, "b": 2}), params_hash({"b": 2, "a": 1}))


class PathCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.workspace = Path(self._temporary.name).resolve()
        self.file = self.workspace / "reports" / "today.md"
        self.file.parent.mkdir(parents=True)
        self.file.write_text("hello", encoding="utf-8")

    def test_read_ok(self):
        target = validate_read_path("reports/today.md", self.workspace)

        self.assertEqual(target, self.file)


    def test_device_names_rejected(self):
        for name in ("NUL", "CON", "con.md", "COM1", "LPT9"):
            with self.subTest(name=name):
                with self.assertRaises(PolicyError):
                    validate_read_path(name, self.workspace)


    def test_device_name_in_nested_segment(self):
        for name in ("NUL/a.md", "safe/CON/b.md"):
            with self.subTest(name=name):
                with self.assertRaises(PolicyError) as caught:
                    validate_read_path(name, self.workspace)

                self.assertIn("设备名", str(caught.exception))

    def test_read_growth_rejected(self):
        from src.qq.policy import read_text_checked
        from unittest.mock import patch as _patch

        original = Path.read_bytes

        def growing(self_path):
            data = original(self_path)
            self_path.write_bytes(data + b"extra")

            return data

        with _patch.object(Path, "read_bytes", growing):
            with self.assertRaises(PolicyError) as caught:
                read_text_checked(self.file)

        self.assertIn("发生变化", str(caught.exception))

    def test_read_text_checked_ok(self):
        from src.qq.policy import read_text_checked

        self.assertEqual(read_text_checked(self.file), "hello")

    def test_missing_file(self):
        with self.assertRaises(PolicyError):
            validate_read_path("nope.md", self.workspace)

    def test_parent_traversal(self):
        for path in ("../x.md", "a/../../x.md"):
            with self.subTest(path=path):
                with self.assertRaises(PolicyError):
                    validate_read_path(path, self.workspace)

    def test_absolute_drive(self):
        with self.assertRaises(PolicyError):
            validate_read_path("C:/Windows/win.ini", self.workspace)

    def test_unc(self):
        with self.assertRaises(PolicyError):
            validate_read_path("\\\\server\\share\\x.md", self.workspace)

    def test_ads(self):
        with self.assertRaises(PolicyError):
            validate_read_path("reports/today.md:stream", self.workspace)


    def test_empty_path(self):
        with self.assertRaises(PolicyError):
            validate_read_path("", self.workspace)

    def test_drive_case_insensitive(self):
        with self.assertRaises(PolicyError):
            validate_read_path("c:/windows/win.ini", self.workspace)

    def test_ads_missing_file(self):
        with self.assertRaises(PolicyError):
            validate_read_path("missing.md:stream", self.workspace)

    def test_directory_rejected(self):
        with self.assertRaises(PolicyError):
            validate_read_path("reports", self.workspace)

    def test_too_large(self):
        big = self.workspace / "big.md"
        big.write_bytes(b"x" * (200 * 1024 + 1))

        with self.assertRaises(PolicyError):
            validate_read_path("big.md", self.workspace)

    def test_symlink_rejected(self):
        link = self.workspace / "link.md"

        try:
            os.symlink(self.file, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink 不可用")

        with self.assertRaises(PolicyError):
            validate_read_path("link.md", self.workspace)


class DecideCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.workspace = Path(self._temporary.name).resolve()
        (self.workspace / "a.md").write_text("x", encoding="utf-8")

    def test_danger_denied(self):
        decision = decide(
            intent="exec", params={}, route_action="deny", workspace=self.workspace
        )

        self.assertEqual(decision["decision"], "deny")

    def test_read_file_requires_confirm(self):
        decision = decide(
            intent="read_file",
            params={"path": "a.md"},
            route_action="confirm",
            workspace=self.workspace,
        )

        self.assertEqual(decision["decision"], "confirm")
        self.assertEqual(len(decision["confirm_id"]), 8)
        self.assertEqual(decision["ttl"], 60)

    def test_read_file_bad_path_denied(self):
        decision = decide(
            intent="read_file",
            params={"path": "../x.md"},
            route_action="confirm",
            workspace=self.workspace,
        )

        self.assertEqual(decision["decision"], "deny")

    def test_status_allowed(self):
        decision = decide(
            intent="status", params={}, route_action="execute", workspace=self.workspace
        )

        self.assertEqual(decision["decision"], "allow")


class TruncateCase(unittest.TestCase):
    def test_short_text_untouched(self):
        self.assertEqual(truncate_for_qq("abc"), "abc")

    def test_long_text_truncated_with_marker(self):
        result = truncate_for_qq("x" * 5000, limit=3000)

        self.assertLessEqual(len(result), 3000 + 20)
        self.assertIn("中间已省略", result)


if __name__ == "__main__":
    unittest.main()
