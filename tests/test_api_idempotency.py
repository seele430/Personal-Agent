
import unittest
import uuid

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from src import api


class TestChatIdempotency(unittest.TestCase):

    def setUp(self):
        # 每个测试使用独立数据库
        # 不接触用户真实的 chat_requests.db
        self.temp_dir = TemporaryDirectory()

        self.db_path = (
            Path(self.temp_dir.name) / "chat_test.db"
        )

        self.db_patch = patch.object(
            api,
            "CHAT_DB_PATH",
            self.db_path
        )

        self.db_patch.start()

        api.init_chat_db()

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def new_id(self):
        return "test_" + uuid.uuid4().hex

    def new_request(self, message="测试消息"):
        return api.ChatRequest(
            message=message,
            request_id=self.new_id()
        )

    # ======================================
    # 1. 同一个请求只调用一次模型
    # ======================================

    def test_duplicate_request_uses_cache(self):
        request = self.new_request()

        with patch.object(
            api,
            "chat",
            return_value="模拟 AI 回复"
        ) as mocked_chat:

            first = api.chat_endpoint(request)
            second = api.chat_endpoint(request)

        self.assertEqual(first, second)
        self.assertEqual(
            first["status"],
            "completed"
        )

        mocked_chat.assert_called_once()

    # ======================================
    # 2. 相同 ID 不允许不同消息
    # ======================================

    def test_same_id_different_message_rejected(self):
        request_id = self.new_id()

        first_request = api.ChatRequest(
            message="第一条消息",
            request_id=request_id
        )

        second_request = api.ChatRequest(
            message="另一条不同消息",
            request_id=request_id
        )

        with patch.object(
            api,
            "chat",
            return_value="已完成"
        ) as mocked_chat:

            api.chat_endpoint(first_request)

            with self.assertRaises(
                HTTPException
            ) as context:
                api.chat_endpoint(second_request)

        self.assertEqual(
            context.exception.status_code,
            409
        )

        mocked_chat.assert_called_once()

    # ======================================
    # 3. 并发重复提交不能重复调用
    # ======================================

    def test_processing_request_returns_202(self):
        request = self.new_request()

        started = Event()
        release = Event()

        def slow_chat(message):
            started.set()

            if not release.wait(timeout=5):
                raise TimeoutError(
                    "模拟任务等待超时"
                )

            return "处理完成"

        with patch.object(
            api,
            "chat",
            side_effect=slow_chat
        ) as mocked_chat:

            with ThreadPoolExecutor(
                max_workers=2
            ) as executor:

                future = executor.submit(
                    api.chat_endpoint,
                    request
                )

                try:
                    self.assertTrue(
                        started.wait(timeout=3)
                    )

                    duplicate = api.chat_endpoint(
                        request
                    )

                    self.assertIsInstance(
                        duplicate,
                        JSONResponse
                    )

                    self.assertEqual(
                        duplicate.status_code,
                        202
                    )

                    self.assertEqual(
                        mocked_chat.call_count,
                        1
                    )

                finally:
                    release.set()

                result = future.result(timeout=5)

        self.assertEqual(
            result["status"],
            "completed"
        )

        self.assertEqual(
            result["reply"],
            "处理完成"
        )

    # ======================================
    # 4. 模型异常不自动重复调用
    # ======================================

    def test_failure_becomes_unknown(self):
        request = self.new_request()

        with patch.object(
            api,
            "chat",
            side_effect=TimeoutError(
                "模拟模型超时"
            )
        ) as mocked_chat:

            first = api.chat_endpoint(request)
            second = api.chat_endpoint(request)

        self.assertIsInstance(
            first,
            JSONResponse
        )

        self.assertEqual(
            first.status_code,
            502
        )

        self.assertEqual(
            second.status_code,
            409
        )

        record = api.read_record(
            request.request_id
        )

        self.assertEqual(
            record["status"],
            "unknown"
        )

        mocked_chat.assert_called_once()

    # ======================================
    # 5. 重启恢复未完成请求
    # ======================================

    def test_restart_marks_processing_unknown(self):
        request = self.new_request()

        api.claim_request(
            request.request_id,
            api.message_digest(request.message)
        )

        # 模拟应用重新初始化数据库
        api.init_chat_db()

        record = api.read_record(
            request.request_id
        )

        self.assertEqual(
            record["status"],
            "unknown"
        )

        with patch.object(
            api,
            "chat"
        ) as mocked_chat:

            result = api.chat_endpoint(request)

        self.assertEqual(
            result.status_code,
            409
        )

        mocked_chat.assert_not_called()

    # ======================================
    # 6. 成功结果可以通过 ID 查询
    # ======================================

    def test_completed_result_can_be_queried(self):
        request = self.new_request()

        with patch.object(
            api,
            "chat",
            return_value="保存好的回答"
        ):
            api.chat_endpoint(request)

        result = api.get_chat_request(
            request.request_id
        )

        self.assertEqual(
            result["status"],
            "completed"
        )

        self.assertEqual(
            result["reply"],
            "保存好的回答"
        )

    # ======================================
    # 7. 旧版前端请求保持兼容
    # ======================================

    def test_legacy_request_without_id(self):
        request = api.ChatRequest(
            message="旧版聊天测试"
        )

        with patch.object(
            api,
            "chat",
            return_value="兼容成功"
        ) as mocked_chat:

            result = api.chat_endpoint(request)

        self.assertEqual(
            result["status"],
            "completed"
        )

        self.assertEqual(
            result["reply"],
            "兼容成功"
        )

        self.assertTrue(
            result["request_id"]
        )

        mocked_chat.assert_called_once()


if __name__ == "__main__":
    unittest.main()