"""测试运行时安全守卫的集成测试。

不调用真实网络。
不访问真实数据库内容。
"""

import socket
import sqlite3
import tempfile
import unittest

from contextlib import closing
from pathlib import Path

from scripts.test_safety import (
    TestSafetyViolation,
    guarded_test_runtime,
)


ROOT = Path(__file__).resolve().parents[2]


class TestRuntimeSafety(unittest.TestCase):

    def test_memory_database_allowed(self):
        with guarded_test_runtime(ROOT):

            with closing(
                sqlite3.connect(":memory:")
            ) as conn:

                result = conn.execute(
                    "SELECT 1"
                ).fetchone()

        self.assertEqual(
            result,
            (1,),
        )

    def test_temporary_database_allowed(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = (
                Path(folder) / "test.db"
            )

            with guarded_test_runtime(ROOT):

                with closing(
                    sqlite3.connect(db_path)
                ) as conn:

                    conn.execute(
                        "CREATE TABLE sample (id INTEGER)"
                    )

                    conn.commit()

            self.assertTrue(
                db_path.is_file()
            )

    def test_project_database_blocked(self):
        # 不要求真实数据库存在。
        # 拦截必须发生在 SQLite 打开文件之前。
        db_path = (
            ROOT / "safety_example.db"
        )

        with guarded_test_runtime(ROOT):

            with self.assertRaises(
                TestSafetyViolation
            ):
                sqlite3.connect(
                    db_path
                )

        self.assertFalse(
            db_path.exists()
        )

    def test_relative_project_path_blocked(self):
        with guarded_test_runtime(ROOT):

            with self.assertRaises(
                TestSafetyViolation
            ):
                sqlite3.connect(
                    "safety_example.db"
                )

    def test_non_temporary_path_blocked(self):
        # 项目目录之外，也不能随便打开文件。
        db_path = (
            ROOT.parent / "safety_example.db"
        )

        with guarded_test_runtime(ROOT):

            with self.assertRaises(
                TestSafetyViolation
            ):
                sqlite3.connect(
                    db_path
                )

    def test_tcp_connection_blocked(self):
        with guarded_test_runtime(ROOT):

            with socket.socket(
                socket.AF_INET,
                socket.SOCK_STREAM,
            ) as sock:

                with self.assertRaises(
                    TestSafetyViolation
                ):
                    sock.connect(
                        ("127.0.0.1", 7890)
                    )

    def test_udp_send_blocked(self):
        with guarded_test_runtime(ROOT):

            with socket.socket(
                socket.AF_INET,
                socket.SOCK_DGRAM,
            ) as sock:

                with self.assertRaises(
                    TestSafetyViolation
                ):
                    sock.sendto(
                        b"test",
                        ("127.0.0.1", 9999),
                    )

    def test_dns_query_blocked(self):
        with guarded_test_runtime(ROOT):

            with self.assertRaises(
                TestSafetyViolation
            ):
                socket.getaddrinfo(
                    "example.invalid",
                    443,
                )

    def test_patches_restored_after_exit(self):
        original_sqlite = sqlite3.connect
        original_socket = socket.socket.connect

        with guarded_test_runtime(ROOT):

            self.assertIsNot(
                sqlite3.connect,
                original_sqlite,
            )

            self.assertIsNot(
                socket.socket.connect,
                original_socket,
            )

        self.assertIs(
            sqlite3.connect,
            original_sqlite,
        )

        self.assertIs(
            socket.socket.connect,
            original_socket,
        )


if __name__ == "__main__":
    unittest.main()