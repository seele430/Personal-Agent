"""/api/auth 兼容前缀回归：前端历史路径必须能打到 auth router。"""

import unittest

from fastapi.testclient import TestClient

from src.main import app


class AuthCompatCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_api_auth_prefix_reaches_router(self):
        self.assertEqual(self.client.get("/api/auth/state").status_code, 200)

    def test_api_auth_prefix_does_not_swallow_other_routes(self):
        self.assertEqual(self.client.get("/api/artifacts").status_code, 401)


if __name__ == "__main__":
    unittest.main()
