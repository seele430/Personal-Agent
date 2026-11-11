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



    def test_console_and_monitor_prefixes_reach_router(self):
        for path in ("/api/console/snapshot", "/api/monitors", "/api/notifications", "/api/usage/summary"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)


if __name__ == "__main__":
    unittest.main()
