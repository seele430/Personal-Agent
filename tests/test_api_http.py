
import unittest
import uuid

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import api


class TestChatHttpAPI(unittest.TestCase):
    """测试 FastAPI 聊天接口与请求幂等行为。"""

    def setUp(self):
        # 每个测试使用独立的临时数据库。
        self.temp_dir = TemporaryDirectory()

        self.db_patch = patch.object(
            api,
            "CHAT_DB_PATH",
            Path(self.temp_dir.name) / "requests_test.db",
        )

        self.db_patch.start()

        # 启动测试客户端并触发数据库初始化。
        self.client = TestClient(api.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.db_patch.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def new_id():
        return "test_" + uuid.uuid4().hex

    # ======================================
    # 1. 健康检查
    # ======================================

    def test_health_route(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"status": "ok"}
        )

    # ======================================
    # 2. 发送消息并查询结果
    # ======================================

    def test_post_and_get_completed_reply(self):
        request_id = self.new_id()

        with patch.object(
            api,
            "chat",
            return_value="模拟回复"
        ) as model:

            response = self.client.post(
                "/chat",
                json={
                    "request_id": request_id,
                    "message": "你好",
                },
            )

        self.assertEqual(response.status_code, 200)

        self.assertEqual(
            response.json()["status"],
            "completed"
        )

        self.assertEqual(
            response.json()["reply"],
            "模拟回复"
        )

        model.assert_called_once_with("你好")

        lookup = self.client.get(
            f"/chat/requests/{request_id}"
        )

        self.assertEqual(lookup.status_code, 200)

        self.assertEqual(
            lookup.json()["reply"],
            "模拟回复"
        )

    # ======================================
    # 3. 重复请求返回缓存结果
    # ======================================

    def test_duplicate_returns_cached_result(self):
        request_id = self.new_id()

        payload = {
            "request_id": request_id,
            "message": "不要重复计费",
        }

        with patch.object(
            api,
            "chat",
            return_value="一次回答"
        ) as model:

            first = self.client.post(
                "/chat",
                json=payload
            )

            second = self.client.post(
                "/chat",
                json=payload
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)

        self.assertEqual(
            first.json(),
            second.json()
        )

        model.assert_called_once()

    # ======================================
    # 4. 相同 ID 不允许不同消息
    # ======================================

    def test_same_id_different_message_is_rejected(self):
        request_id = self.new_id()

        with patch.object(
            api,
            "chat",
            return_value="第一条回复"
        ) as model:

            first = self.client.post(
                "/chat",
                json={
                    "request_id": request_id,
                    "message": "第一条",
                },
            )

            second = self.client.post(
                "/chat",
                json={
                    "request_id": request_id,
                    "message": "另一条",
                },
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)

        model.assert_called_once()

    # ======================================
    # 5. 模型异常不能重复执行
    # ======================================

    def test_model_exception_does_not_trigger_retry(self):
        request_id = self.new_id()

        payload = {
            "request_id": request_id,
            "message": "模拟超时",
        }

        with patch.object(
            api,
            "chat",
            side_effect=TimeoutError("测试超时")
        ) as model:

            first = self.client.post(
                "/chat",
                json=payload
            )

            second = self.client.post(
                "/chat",
                json=payload
            )

        self.assertEqual(first.status_code, 502)

        self.assertEqual(
            first.json()["status"],
            "unknown"
        )

        self.assertEqual(second.status_code, 409)

        model.assert_called_once()

        lookup = self.client.get(
            f"/chat/requests/{request_id}"
        )

        self.assertEqual(lookup.status_code, 200)

        self.assertEqual(
            lookup.json()["status"],
            "unknown"
        )

        self.assertIsNone(
            lookup.json()["reply"]
        )

    # ======================================
    # 6. 正在处理中的重复请求返回 202
    # ======================================

    def test_existing_processing_request_returns_202(self):
        request_id = self.new_id()
        message = "仍在处理"

        api.claim_request(
            request_id,
            api.message_digest(message),
        )

        with patch.object(api, "chat") as model:

            response = self.client.post(
                "/chat",
                json={
                    "request_id": request_id,
                    "message": message,
                },
            )

        self.assertEqual(response.status_code, 202)

        self.assertEqual(
            response.json()["status"],
            "processing"
        )

        model.assert_not_called()

    # ======================================
    # 7. 无效请求与不存在的记录
    # ======================================

    def test_invalid_request_id_and_missing_record(self):
        invalid = self.client.post(
            "/chat",
            json={
                "request_id": "bad!",
                "message": "测试",
            },
        )

        self.assertEqual(invalid.status_code, 400)

        missing = self.client.get(
            f"/chat/requests/{self.new_id()}"
        )

        self.assertEqual(missing.status_code, 404)

    # ======================================
    # 8. 兼容旧版前端
    # ======================================

    def test_legacy_request_still_works(self):

        with patch.object(
            api,
            "chat",
            return_value="兼容旧前端"
        ) as model:

            response = self.client.post(
                "/chat",
                json={
                    "message": "旧格式请求",
                },
            )

        self.assertEqual(response.status_code, 200)

        self.assertEqual(
            response.json()["reply"],
            "兼容旧前端"
        )

        self.assertTrue(
            response.json()["request_id"]
        )

        model.assert_called_once()


if __name__ == "__main__":
    unittest.main()