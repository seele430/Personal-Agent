"""正式聊天 API 的 request_id 校验测试。

使用临时数据库、模拟认证及模拟 AI。
旧入口 src.api:app 下线后，由本文件保护 400 校验。
"""

import unittest

from contextlib import closing
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from tests.helper import isolate_paths
from src import api
from src.auth_api import get_current_user
from src.main import app


class TestChatRequestValidationHTTP(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()

        isolate_paths(self, self.temp)

        self.old_overrides = app.dependency_overrides.copy()

        def fake_user(request: Request):
            if request.headers.get("authorization", "") != "Bearer test-token":
                raise HTTPException(status_code=401, detail="请先登录")

            return {
                "id": "validation-test-user",
                "username": "testowner",
                "role": "owner",
                "active": True,
            }

        app.dependency_overrides[get_current_user] = fake_user

        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)

        app.dependency_overrides.clear()
        app.dependency_overrides.update(self.old_overrides)

        self.temp.cleanup()

    def record_count(self):
        with closing(api.connect_db()) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM chat_requests"
            ).fetchone()[0]

    def test_invalid_request_id_returns_400_without_model_call(self):
        with patch.object(api, "chat") as model:
            response = self.client.post(
                "/chat",
                headers={"Authorization": "Bearer test-token"},
                json={
                    "request_id": "bad!",
                    "message": "测试",
                },
            )

        self.assertEqual(response.status_code, 400)

        # 校验失败不能调用模型，也不能占用记录或预算。
        model.assert_not_called()
        self.assertEqual(self.record_count(), 0)


if __name__ == "__main__":
    unittest.main()