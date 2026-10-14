"""init_chat_db 重启恢复测试。

使用临时数据库，不启动 HTTP，不调用模型。
main.py 启动时调用 init_chat_db，旧入口下线后由本文件保护这条逻辑。
"""

import unittest

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src import api


TIMESTAMP = "2026-10-12T12:00:00+00:00"


class TestChatDbRestart(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()

        self.db_patch = patch.object(
            api,
            "CHAT_DB_PATH",
            Path(self.temp.name) / "chat_restart_test.db",
        )
        self.db_patch.start()

        # 第一次启动：建表
        api.init_chat_db()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def insert(self, request_id, status, reply=None):
        with closing(api.connect_db()) as conn, conn:
            conn.execute(
                """
                INSERT INTO chat_requests (
                    request_id,
                    message_hash,
                    status,
                    reply,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    "digest-123",
                    status,
                    reply,
                    TIMESTAMP,
                    TIMESTAMP,
                ),
            )

    def read(self, request_id):
        with closing(api.connect_db()) as conn:
            row = conn.execute(
                """
                SELECT status, reply
                FROM chat_requests
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()

        return row[0], row[1]

    def test_restart_marks_processing_unknown(self):
        # 进程在模型调用中途退出，留下 processing 记录。
        self.insert("req_processing", "processing")

        # 第二次启动
        api.init_chat_db()

        # 结果未知，不能当成可以重新调用模型。
        self.assertEqual(
            self.read("req_processing"),
            ("unknown", None),
        )

    def test_restart_keeps_completed_reply(self):
        self.insert("req_completed", "completed", "已有回复")

        api.init_chat_db()

        self.assertEqual(
            self.read("req_completed"),
            ("completed", "已有回复"),
        )


if __name__ == "__main__":
    unittest.main()