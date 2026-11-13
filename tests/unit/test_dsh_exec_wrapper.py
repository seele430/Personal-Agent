"""dsh_exec_wrapper：cmd /c quoting、stdin/stdout/stderr 落盘、退出码透传。"""

from __future__ import annotations

import subprocess
import sys
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory

WRAPPER = (
    Path(__file__).resolve().parents[2] / "scripts" / "dsh_exec_wrapper.py"
)


def run_wrapper(dsh: str, cwd: Path, *args: str) -> tuple[int, Path, Path, Path]:
    stdin = cwd / "in.txt"
    stdout = cwd / "out.txt"
    stderr = cwd / "err.txt"
    stdin.write_text("input", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable, str(WRAPPER),
            "--dsh", dsh,
            "--cwd", str(cwd),
            "--stdin-file", str(stdin),
            "--stdout-file", str(stdout),
            "--stderr-file", str(stderr),
            "--", *args,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    return completed.returncode, stdout, stderr, stdin


class DshExecWrapperTest(unittest.TestCase):
    def test_python_roundtrip(self):
        """宿主上跑 python -c print(1)：文件内容与退出码必须正确。"""

        with TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            code, stdout, stderr, _ = run_wrapper(
                sys.executable, Path(temp), "-c", "print(1)"
            )

            self.assertEqual(code, 0)
            self.assertEqual(stdout.read_text().strip(), "1")
            self.assertEqual(stderr.read_text(), "")

    def test_exit_code_passthrough(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            code, _, _, _ = run_wrapper(
                sys.executable, Path(temp), "-c", "import sys; sys.exit(7)"
            )

            self.assertEqual(code, 7)

    def test_cmd_with_space_in_path(self):
        """带空格路径的 .cmd：cmd /c 的剥引号规则不能把它拆坏。"""

        with TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            folder = Path(temp) / "dir with space"
            folder.mkdir()
            script = folder / "tool.cmd"
            script.write_text("@echo off\r\necho CMD_OK\r\n", encoding="utf-8")

            code, stdout, _, _ = run_wrapper(str(script), Path(temp))

            self.assertEqual(code, 0)
            self.assertIn("CMD_OK", stdout.read_text())

    def test_missing_executable(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            code, _, _, _ = run_wrapper(
                str(Path(temp) / "nope.cmd"), Path(temp)
            )

            self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
