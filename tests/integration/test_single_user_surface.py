"""单用户产品面：只有站长登录，没有注册/邀请码 HTTP 入口。"""

from __future__ import annotations

import unittest

from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from src.main import app
from tests.helper import isolate_paths


class SingleUserSurfaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)

        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_invite_and_register_routes_are_not_exposed(self):
        for path in ("/auth/register", "/auth/invite", "/auth/invites"):
            with self.subTest(path=path):
                response = self.client.post(path, json={})

                # 没有注册路由：未知 POST 要么 404，要么被 SPA 兜底判成 405；
                # 两种都表示「这个功能不存在」，绝不能成功。
                self.assertIn(response.status_code, {404, 405}, response.text)

    def test_state_endpoint_still_works(self):
        response = self.client.get("/auth/state")

        self.assertEqual(response.status_code, 200, response.text)

        body = response.json()

        self.assertIn("initialized", body)
        self.assertIn("registration_open", body)


if __name__ == "__main__":
    unittest.main()
